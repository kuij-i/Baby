"""Deterministic, fail-closed recovery for stale workers and lost task leases.

Rules (all applied inside one ``BEGIN IMMEDIATE`` transaction per pass):

1. A live worker (STARTING/IDLE/BUSY/STOPPING) whose ``last_heartbeat_at`` is older than
   ``worker_stale_timeout_seconds`` is marked STALE. Its identity is revoked: it can no
   longer heartbeat, claim, renew or finalize.
2. A lease is revoked when it has expired (``lease_expires_at < now``) or when its worker
   is no longer live (STALE, ERROR, STOPPED).
3. The task behind a revoked lease, if still IN_PROGRESS, moves IN_PROGRESS → FAILED with an
   explicit recovery reason. It is never re-queued and never replayed automatically:
   interrupted work may have performed privileged or side-effecting operations.

Recovery is idempotent and safe to run concurrently from the API, supervisors and workers.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

from baby.audit import audit_log
from baby.core import AuditEventType, TaskRecord, TaskStatus, WorkerStatus, ensure_valid_transition
from baby.logging import get_logger
from baby.observability.metrics import metrics_collector
from baby.persistence.sqlite_tasks import SQLiteTaskStore
from baby.persistence.sqlite_workers import LIVE_WORKER_STATUSES, SQLiteWorkerStore, ts
from baby.workers.queue import utcnow

logger = get_logger(__name__)

# Bounded set of recovery reasons (used as a metric label).
REASON_LEASE_EXPIRED = "lease_expired"
REASON_WORKER_STALE = "worker_stale"
REASON_WORKER_EXITED = "worker_exited"
REASON_WORKER_STOPPED = "worker_stopped"


@dataclass
class RecoveryReport:
    """Outcome of one recovery pass."""

    stale_workers: List[str] = field(default_factory=list)
    failed_tasks: List[TaskRecord] = field(default_factory=list)
    released_leases: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.stale_workers or self.failed_tasks or self.released_leases)


class WorkerRecovery:
    """Detects stale workers and fails their interrupted tasks closed."""

    def __init__(
        self,
        task_store: Optional[SQLiteTaskStore] = None,
        worker_store: Optional[SQLiteWorkerStore] = None,
        stale_timeout_seconds: Optional[float] = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        from baby.configuration import settings

        self._tasks = task_store or SQLiteTaskStore()
        self._workers = worker_store or SQLiteWorkerStore(self._tasks.db)
        self._db = self._tasks.db
        self._stale_timeout = timedelta(
            seconds=(
                stale_timeout_seconds if stale_timeout_seconds is not None else settings.worker_stale_timeout_seconds
            )
        )
        self._clock = clock

    def recover(self, now: Optional[datetime] = None) -> RecoveryReport:
        """Mark stale workers and revoke expired/orphaned leases (fail closed)."""
        now = now or self._clock()
        report = RecoveryReport()
        live = [s.value for s in LIVE_WORKER_STATUSES]
        placeholders = ",".join("?" for _ in live)

        with self._db.immediate_transaction() as conn:
            cutoff = ts(now - self._stale_timeout)
            stale_rows = conn.execute(
                f"SELECT worker_id FROM workers WHERE status IN ({placeholders}) AND last_heartbeat_at < ?",
                (*live, cutoff),
            ).fetchall()
            for row in stale_rows:
                conn.execute(
                    "UPDATE workers SET status = ?, stopped_at = ?, last_error = ? WHERE worker_id = ?",
                    (WorkerStatus.STALE.value, ts(now), "Heartbeat timeout", row["worker_id"]),
                )
                report.stale_workers.append(row["worker_id"])

            lease_rows = conn.execute(
                f"""
                SELECT l.task_id, l.worker_id, l.lease_expires_at, w.status AS worker_status
                FROM task_leases l JOIN workers w ON w.worker_id = l.worker_id
                WHERE l.lease_expires_at < ? OR w.status NOT IN ({placeholders})
                """,
                (ts(now), *live),
            ).fetchall()
            revoked: List[Tuple[str, str, str]] = []
            for row in lease_rows:
                if row["worker_status"] == WorkerStatus.STALE.value:
                    reason = REASON_WORKER_STALE
                elif row["worker_status"] in (WorkerStatus.ERROR.value,):
                    reason = REASON_WORKER_EXITED
                elif row["worker_status"] == WorkerStatus.STOPPED.value:
                    reason = REASON_WORKER_STOPPED
                else:
                    reason = REASON_LEASE_EXPIRED
                revoked.append((row["task_id"], row["worker_id"], reason))

            for task_id_str, worker_id, reason in revoked:
                self._revoke_lease(conn, task_id_str, worker_id, reason, now, report)

        self._emit(report)
        return report

    def recover_worker(
        self,
        worker_id: str,
        reason: str = REASON_WORKER_EXITED,
        now: Optional[datetime] = None,
        error: Optional[str] = None,
    ) -> RecoveryReport:
        """Recover one worker whose process is known to be gone (e.g. supervisor saw it exit).

        A worker still in a live status is marked ERROR; every lease it holds is revoked and
        its IN_PROGRESS tasks fail closed.
        """
        now = now or self._clock()
        report = RecoveryReport()
        live = [s.value for s in LIVE_WORKER_STATUSES]
        with self._db.immediate_transaction() as conn:
            cur = conn.execute(
                f"UPDATE workers SET status = ?, stopped_at = ?, last_error = ?, current_task_id = NULL "
                f"WHERE worker_id = ? AND status IN ({','.join('?' for _ in live)})",
                (WorkerStatus.ERROR.value, ts(now), error or "Worker process exited", worker_id, *live),
            )
            if cur.rowcount:
                report.stale_workers.append(worker_id)
            rows = conn.execute("SELECT task_id FROM task_leases WHERE worker_id = ?", (worker_id,)).fetchall()
            for row in rows:
                self._revoke_lease(conn, row["task_id"], worker_id, reason, now, report)
        self._emit(report, worker_reason=reason)
        return report

    def _revoke_lease(self, conn, task_id_str: str, worker_id: str, reason: str, now: datetime, report: RecoveryReport):  # type: ignore[no-untyped-def]
        from uuid import UUID

        from baby.core import TaskId

        task_id = TaskId(id=UUID(task_id_str))
        conn.execute("DELETE FROM task_leases WHERE task_id = ?", (task_id_str,))
        conn.execute(
            "UPDATE workers SET current_task_id = NULL WHERE worker_id = ? AND current_task_id = ?",
            (worker_id, task_id_str),
        )
        report.released_leases += 1
        record = self._tasks.read_record(conn, task_id)
        if record is None or record.status != TaskStatus.IN_PROGRESS:
            return
        ensure_valid_transition(task_id, record.status, TaskStatus.FAILED)
        record.status = TaskStatus.FAILED
        record.error = f"Worker '{worker_id}' lost its claim ({reason}); task failed closed and was not replayed"
        record.updated_at = now
        record.completed_at = now
        self._tasks.write_record(conn, record)
        SQLiteWorkerStore.record_task_outcome(conn, worker_id, success=False)
        report.failed_tasks.append(record)
        metrics_collector.increment_counter("worker_tasks_recovered_total", labels={"reason": reason})
        duration_ms = (now - record.created_at).total_seconds() * 1000
        metrics_collector.record_task_failed(duration_ms, error_type=reason)

    def _emit(self, report: RecoveryReport, worker_reason: str = REASON_WORKER_STALE) -> None:
        for worker_id in report.stale_workers:
            logger.warning("Worker marked lost", worker_id=worker_id, reason=worker_reason)
            metrics_collector.increment_counter("workers_lost_total", labels={"reason": worker_reason})
            audit_log.record(
                AuditEventType.WORKER_STALE,
                details={"worker_id": worker_id, "reason": worker_reason},
            )
        for record in report.failed_tasks:
            logger.warning("Task failed closed by worker recovery", task_id=str(record.task_id))
            details = {"worker_id": record.assigned_worker_id, "error": record.error, "replayed": False}
            for event in (AuditEventType.TASK_RECOVERED, AuditEventType.TASK_FAILED):
                audit_log.record(event, task_id=record.task_id, user_id=record.user_id, details=details)
