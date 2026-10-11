"""Durable SQLite task queue: submission, atomic claiming, fenced leases and finalization.

There is no separate queue table: a task is queued when its durable ``task_records``
row is PENDING (or AWAITING_APPROVAL with every approval request decided and an
unconsumed grant). Ownership is the ``task_leases`` row.

Claim protocol (one ``BEGIN IMMEDIATE`` transaction, so it is atomic across processes):
    1. pick the highest-priority, oldest eligible task with no lease row;
    2. validate the transition with the authoritative state machine
       (PENDING → IN_PROGRESS or AWAITING_APPROVAL → IN_PROGRESS);
    3. increment ``claim_count`` and use it as the lease ``fence``;
    4. insert the lease (``task_id`` is the PRIMARY KEY: a second lease cannot exist).

Every later write by the worker (renewal, completion, failure, parking for approval)
presents ``(worker_id, fence)`` and is rejected with ``LeaseLostError`` once the lease
has expired, was revoked by recovery, or was re-issued — the worker can never record a
result for a task it no longer owns.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional
from uuid import UUID

from baby.core import (
    AgentResult,
    Task,
    TaskId,
    TaskLease,
    TaskRecord,
    TaskStatus,
    WorkerStatus,
    ensure_valid_transition,
    utc_now,
)
from baby.errors import InvalidStateTransitionError, LeaseLostError, TaskNotFoundError
from baby.logging import get_logger
from baby.observability.metrics import metrics_collector
from baby.persistence.sqlite_tasks import SQLiteTaskStore
from baby.persistence.sqlite_workers import LIVE_WORKER_STATUSES, SQLiteWorkerStore, parse_ts, ts

logger = get_logger(__name__)


def utcnow() -> datetime:
    return utc_now()


@dataclass(frozen=True)
class ClaimedTask:
    """A task exclusively claimed by a worker together with its lease."""

    record: TaskRecord
    lease: TaskLease
    resumed_after_approval: bool

    def to_task(self) -> Task:
        r = self.record
        return Task(
            id=r.task_id,
            title=r.title,
            description=r.description,
            created_at=r.created_at,
            user_id=r.user_id,
            priority=r.priority,
        )


@dataclass(frozen=True)
class LeaseState:
    """Result of a lease renewal attempt."""

    held: bool
    cancel_requested: bool = False
    lease_expires_at: Optional[datetime] = None


def approvals_ready(record: TaskRecord, now: Optional[datetime] = None) -> bool:
    """AWAITING_APPROVAL task may resume: nothing undecided and an unexpired, unconsumed grant exists.

    A task with an expired approval never resumes; recovery fails it closed.
    """
    from baby.permissions.approvals import approval_ttl, expired_approval_reason

    if not record.approvals:
        return False
    if any(a.approved is None for a in record.approvals):
        return False
    if expired_approval_reason(record, now or utcnow(), approval_ttl()) is not None:
        return False
    return any(a.approved is True and a.consumed_at is None for a in record.approvals)


class DurableTaskQueue:
    """Cross-process task queue backed by the existing SQLite transaction infrastructure."""

    def __init__(
        self,
        task_store: Optional[SQLiteTaskStore] = None,
        worker_store: Optional[SQLiteWorkerStore] = None,
        lease_duration_seconds: Optional[float] = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        from baby.configuration import settings

        self._tasks = task_store or SQLiteTaskStore()
        self._workers = worker_store or SQLiteWorkerStore(self._tasks.db)
        self._db = self._tasks.db
        self._lease_duration = timedelta(
            seconds=(
                lease_duration_seconds if lease_duration_seconds is not None else settings.task_lease_duration_seconds
            )
        )
        self._clock = clock

    @property
    def task_store(self) -> SQLiteTaskStore:
        return self._tasks

    @property
    def worker_store(self) -> SQLiteWorkerStore:
        return self._workers

    # ------------------------------------------------------------------
    # Submission
    # ------------------------------------------------------------------

    def submit(self, task: Task) -> TaskRecord:
        """Durably enqueue a task as PENDING. Nothing is executed by the caller."""
        now = self._clock()
        record = TaskRecord(
            task_id=task.id,
            title=task.title,
            description=task.description or "",
            status=TaskStatus.PENDING,
            created_at=task.created_at or now,
            updated_at=now,
            user_id=task.user_id,
            priority=task.priority,
        )
        with self._db.immediate_transaction() as conn:
            if self._tasks.read_record(conn, task.id) is not None:
                raise InvalidStateTransitionError(f"Task {task.id} already exists")
            self._tasks.write_record(conn, record)
        metrics_collector.record_task_created(task.priority)
        return record

    # ------------------------------------------------------------------
    # Claiming
    # ------------------------------------------------------------------

    def claim_next(self, worker_id: str, now: Optional[datetime] = None) -> Optional[ClaimedTask]:
        """Atomically claim the next eligible task for ``worker_id`` (or return None).

        Only a registered, live worker can claim. The whole operation is one
        ``BEGIN IMMEDIATE`` transaction: two workers can never claim the same task.
        """
        now = now or self._clock()
        expires = now + self._lease_duration
        live = [s.value for s in LIVE_WORKER_STATUSES]

        with self._db.immediate_transaction() as conn:
            worker = conn.execute("SELECT status FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
            if worker is None or worker["status"] not in live or worker["status"] == WorkerStatus.STOPPING.value:
                return None

            candidates: List[TaskRecord] = []
            pending = conn.execute(
                """
                SELECT t.task_id FROM task_records t
                LEFT JOIN task_leases l ON l.task_id = t.task_id
                WHERE l.task_id IS NULL AND t.status = ? AND t.cancel_requested = 0
                ORDER BY t.priority DESC, t.created_at ASC LIMIT 1
                """,
                (TaskStatus.PENDING.value,),
            ).fetchone()
            if pending is not None:
                rec = self._tasks.read_record(conn, TaskId(id=UUID(pending["task_id"])))
                if rec is not None:
                    candidates.append(rec)
            for row in conn.execute(
                """
                SELECT t.task_id FROM task_records t
                LEFT JOIN task_leases l ON l.task_id = t.task_id
                WHERE l.task_id IS NULL AND t.status = ? AND t.cancel_requested = 0
                ORDER BY t.priority DESC, t.created_at ASC LIMIT 200
                """,
                (TaskStatus.AWAITING_APPROVAL.value,),
            ).fetchall():
                rec = self._tasks.read_record(conn, TaskId(id=UUID(row["task_id"])))
                if rec is not None and approvals_ready(rec, now):
                    candidates.append(rec)
            if not candidates:
                return None

            record = min(candidates, key=lambda r: (-r.priority, r.created_at))
            resumed = record.status == TaskStatus.AWAITING_APPROVAL
            ensure_valid_transition(record.task_id, record.status, TaskStatus.IN_PROGRESS)
            record.status = TaskStatus.IN_PROGRESS
            record.assigned_worker_id = worker_id
            record.updated_at = now
            self._tasks.write_record(conn, record)

            # (No RETURNING clause: keeps compatibility with SQLite < 3.35.)
            task_key = str(record.task_id)
            conn.execute("UPDATE task_records SET claim_count = claim_count + 1 WHERE task_id = ?", (task_key,))
            fence = int(
                conn.execute("SELECT claim_count FROM task_records WHERE task_id = ?", (task_key,)).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO task_leases (task_id, worker_id, fence, claimed_at, renewed_at, lease_expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (str(record.task_id), worker_id, fence, ts(now), ts(now), ts(expires)),
            )
            conn.execute(
                "UPDATE workers SET status = ?, current_task_id = ? WHERE worker_id = ?",
                (WorkerStatus.BUSY.value, str(record.task_id), worker_id),
            )

        metrics_collector.record_task_started()
        metrics_collector.increment_counter("worker_task_claims_total")
        lease = TaskLease(
            task_id=record.task_id,
            worker_id=worker_id,
            fence=fence,
            claimed_at=now,
            renewed_at=now,
            lease_expires_at=expires,
        )
        return ClaimedTask(record=record, lease=lease, resumed_after_approval=resumed)

    # ------------------------------------------------------------------
    # Lease maintenance
    # ------------------------------------------------------------------

    @staticmethod
    def _lease_row_valid(row, worker_id: str, fence: int, now: datetime) -> bool:  # type: ignore[no-untyped-def]
        return (
            row is not None
            and row["worker_id"] == worker_id
            and int(row["fence"]) == fence
            and parse_ts(row["lease_expires_at"]) is not None
            and parse_ts(row["lease_expires_at"]) >= now  # type: ignore[operator]
        )

    def renew(self, task_id: TaskId, worker_id: str, fence: int, now: Optional[datetime] = None) -> LeaseState:
        """Extend the lease if (and only if) this worker still holds it with this fence."""
        now = now or self._clock()
        with self._db.immediate_transaction() as conn:
            row = conn.execute(
                """
                SELECT l.worker_id, l.fence, l.lease_expires_at, t.cancel_requested, t.status
                FROM task_leases l JOIN task_records t ON t.task_id = l.task_id
                WHERE l.task_id = ?
                """,
                (str(task_id),),
            ).fetchone()
            if not self._lease_row_valid(row, worker_id, fence, now) or row["status"] != TaskStatus.IN_PROGRESS.value:
                return LeaseState(held=False)
            expires = now + self._lease_duration
            conn.execute(
                "UPDATE task_leases SET renewed_at = ?, lease_expires_at = ? WHERE task_id = ?",
                (ts(now), ts(expires), str(task_id)),
            )
        metrics_collector.increment_counter("worker_task_lease_renewals_total")
        return LeaseState(held=True, cancel_requested=bool(row["cancel_requested"]), lease_expires_at=expires)

    def get_lease(self, task_id: TaskId) -> Optional[TaskLease]:
        with self._db.transaction() as conn:
            row = conn.execute("SELECT * FROM task_leases WHERE task_id = ?", (str(task_id),)).fetchone()
        if row is None:
            return None
        return TaskLease(
            task_id=task_id,
            worker_id=row["worker_id"],
            fence=row["fence"],
            claimed_at=datetime.fromisoformat(row["claimed_at"]),
            renewed_at=datetime.fromisoformat(row["renewed_at"]),
            lease_expires_at=datetime.fromisoformat(row["lease_expires_at"]),
        )

    # ------------------------------------------------------------------
    # Fenced terminal writes
    # ------------------------------------------------------------------

    def _fenced_transition(
        self,
        task_id: TaskId,
        worker_id: str,
        fence: int,
        status: TaskStatus,
        now: Optional[datetime],
        result: Optional[AgentResult],
        error: Optional[str],
        count_outcome: bool,
    ) -> TaskRecord:
        now = now or self._clock()
        with self._db.immediate_transaction() as conn:
            row = conn.execute(
                "SELECT worker_id, fence, lease_expires_at FROM task_leases WHERE task_id = ?", (str(task_id),)
            ).fetchone()
            if not self._lease_row_valid(row, worker_id, fence, now):
                raise LeaseLostError(
                    f"Worker '{worker_id}' no longer holds the lease for task {task_id} (fence {fence})"
                )
            record = self._tasks.read_record(conn, task_id)
            if record is None:
                raise TaskNotFoundError(f"Task {task_id} not found")
            ensure_valid_transition(task_id, record.status, status)
            record.status = status
            record.updated_at = now
            if result is not None:
                record.result = result
                record.assigned_agent_id = result.agent_id
            if error is not None:
                record.error = error
            elif status == TaskStatus.COMPLETED:
                # Clear an error left by an earlier approval pause; a completed task has none.
                record.error = None
            if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                record.completed_at = now
            self._tasks.write_record(conn, record)
            conn.execute("DELETE FROM task_leases WHERE task_id = ?", (str(task_id),))
            if count_outcome:
                SQLiteWorkerStore.record_task_outcome(conn, worker_id, success=status == TaskStatus.COMPLETED)
            conn.execute(
                "UPDATE workers SET status = ?, current_task_id = NULL WHERE worker_id = ? AND status = ?",
                (WorkerStatus.IDLE.value, worker_id, WorkerStatus.BUSY.value),
            )

        duration_ms = (now - record.created_at).total_seconds() * 1000
        if status == TaskStatus.COMPLETED:
            metrics_collector.record_task_completed(duration_ms)
        elif status == TaskStatus.FAILED:
            metrics_collector.record_task_failed(duration_ms, error_type="worker_failure")
        elif status == TaskStatus.CANCELLED:
            metrics_collector.record_task_failed(duration_ms, error_type="cancelled")
        elif status == TaskStatus.AWAITING_APPROVAL:
            metrics_collector.record_approval_requested()
        return record

    def finalize(
        self,
        task_id: TaskId,
        worker_id: str,
        fence: int,
        status: TaskStatus,
        now: Optional[datetime] = None,
        result: Optional[AgentResult] = None,
        error: Optional[str] = None,
    ) -> TaskRecord:
        """Write a terminal status (COMPLETED/FAILED/CANCELLED) while holding the lease.

        Raises:
            LeaseLostError: The worker no longer owns the task; nothing is written.
            InvalidStateTransitionError: The state machine rejects the transition.
        """
        if status not in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            raise InvalidStateTransitionError(f"finalize() requires a terminal status, got '{status.value}'")
        return self._fenced_transition(task_id, worker_id, fence, status, now, result, error, count_outcome=True)

    def park_for_approval(
        self,
        task_id: TaskId,
        worker_id: str,
        fence: int,
        now: Optional[datetime] = None,
        result: Optional[AgentResult] = None,
    ) -> TaskRecord:
        """IN_PROGRESS → AWAITING_APPROVAL and release the lease (worker stops privileged work)."""
        return self._fenced_transition(
            task_id,
            worker_id,
            fence,
            TaskStatus.AWAITING_APPROVAL,
            now,
            result,
            error=result.error if result else "Awaiting approval",
            count_outcome=False,
        )

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------

    def request_cancel(self, task_id: TaskId, now: Optional[datetime] = None) -> TaskRecord:
        """Cancel a task through the state machine.

        PENDING / AWAITING_APPROVAL tasks are cancelled immediately. For an IN_PROGRESS
        task a durable ``cancel_requested`` flag is set; the owning worker observes it at
        its next lease renewal, stops execution and records CANCELLED.

        Raises:
            TaskNotFoundError: Unknown task.
            InvalidStateTransitionError: Task already terminal.
        """
        now = now or self._clock()
        with self._db.immediate_transaction() as conn:
            record = self._tasks.read_record(conn, task_id)
            if record is None:
                raise TaskNotFoundError(f"Task {task_id} not found")
            if record.status == TaskStatus.IN_PROGRESS:
                record.cancel_requested = True
            else:
                ensure_valid_transition(task_id, record.status, TaskStatus.CANCELLED)
                record.status = TaskStatus.CANCELLED
                record.error = "Cancelled before execution"
                record.completed_at = now
            record.updated_at = now
            self._tasks.write_record(conn, record)
        return record
