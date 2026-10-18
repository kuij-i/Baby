"""Worker runtime: the loop that runs inside one isolated worker process.

Startup (before any task is touched):
    1. register a new, unique worker identity (STARTING);
    2. run lease-based recovery for stale workers/expired leases;
    3. start the heartbeat thread;
    4. mark IDLE and begin polling/claiming.

Per claimed task, a watchdog renews the lease every ``worker_heartbeat_interval_seconds``
and stops execution (asyncio cancellation) when:
    - the lease cannot be renewed (expired, revoked, re-issued, or DB failure) → no result
      is written; recovery fails the task closed;
    - cancellation was requested → CANCELLED;
    - ``worker_max_task_seconds`` is exceeded → FAILED;
    - shutdown was requested → FAILED with an explicit shutdown reason.
Results are written only through fenced queue operations while the lease is held.

Shutdown: stop claiming, stop the in-flight task as above, stop heartbeating, mark the
worker STOPPED (or ERROR after a fatal heartbeat loss) and record WORKER_STOPPED.
"""

import asyncio
import os
import platform
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional, Protocol
from uuid import uuid4

from baby.agents import agent_registry
from baby.audit import audit_log
from baby.core import AgentResult, AuditEventType, Task, TaskStatus, WorkerStatus
from baby.errors import LeaseLostError
from baby.logging import get_logger
from baby.observability.metrics import metrics_collector
from baby.persistence.sqlite_tasks import SQLiteTaskStore
from baby.persistence.sqlite_workers import SQLiteWorkerStore
from baby.workers.queue import ClaimedTask, DurableTaskQueue, LeaseState, utcnow
from baby.workers.recovery import WorkerRecovery

logger = get_logger(__name__)

TaskExecutor = Callable[[Task], Awaitable[Optional[AgentResult]]]


class StopSignal(Protocol):
    def is_set(self) -> bool: ...


def new_worker_id() -> str:
    """Unique, non-reused worker identity (contains no host or user information)."""
    return f"worker-{uuid4().hex}"


@dataclass(frozen=True)
class WorkerRuntimeConfig:
    """Timing and identity configuration for one worker runtime."""

    worker_id: str
    poll_interval: float
    heartbeat_interval: float
    stale_timeout: float
    lease_duration: float
    max_task_seconds: float
    slot: int = 0
    max_tasks: Optional[int] = None  # stop after N tasks (tests / batch mode); None = run until stopped

    @classmethod
    def from_settings(cls, worker_id: Optional[str] = None, **overrides: Any) -> "WorkerRuntimeConfig":
        from baby.configuration import settings

        values = dict(
            worker_id=worker_id or new_worker_id(),
            poll_interval=settings.worker_poll_interval_seconds,
            heartbeat_interval=settings.worker_heartbeat_interval_seconds,
            stale_timeout=settings.worker_stale_timeout_seconds,
            lease_duration=settings.task_lease_duration_seconds,
            max_task_seconds=settings.worker_max_task_seconds,
        )
        values.update(overrides)
        return cls(**values)  # type: ignore[arg-type]


async def _default_executor(task: Task) -> Optional[AgentResult]:
    from baby.orchestration.orchestrator import orchestrator

    return await orchestrator.execute_claimed_task(task)


class WorkerRuntime:
    """Claims and executes durable tasks for one worker identity."""

    def __init__(
        self,
        config: WorkerRuntimeConfig,
        task_store: Optional[SQLiteTaskStore] = None,
        worker_store: Optional[SQLiteWorkerStore] = None,
        executor: Optional[TaskExecutor] = None,
        stop_signal: Optional[StopSignal] = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config
        self._tasks = task_store or SQLiteTaskStore()
        self._workers = worker_store or SQLiteWorkerStore(self._tasks.db)
        self._queue = DurableTaskQueue(self._tasks, self._workers, config.lease_duration, clock=clock)
        self._recovery = WorkerRecovery(self._tasks, self._workers, config.stale_timeout, clock=clock)
        self._executor = executor or _default_executor
        self._external_stop = stop_signal
        self._clock = clock
        self._stop = threading.Event()
        self._fatal: Optional[str] = None
        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        self._started = False
        self.tasks_processed = 0

    @property
    def worker_id(self) -> str:
        return self.config.worker_id

    @property
    def queue(self) -> DurableTaskQueue:
        return self._queue

    @property
    def fatal_error(self) -> Optional[str]:
        return self._fatal

    def request_stop(self) -> None:
        """Ask the runtime to stop claiming and shut down gracefully."""
        self._stop.set()

    def stop_requested(self) -> bool:
        return (
            self._stop.is_set()
            or self._fatal is not None
            or bool(self._external_stop is not None and self._external_stop.is_set())
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def startup(self) -> None:
        """Register identity, recover stale state and start heartbeating. Raises on failure."""
        now = self._clock()
        self._workers.register(
            self.worker_id,
            hostname=socket.gethostname(),
            pid=os.getpid(),
            now=now,
            metadata={
                "platform": platform.system(),
                "python": platform.python_version(),
                "slot": self.config.slot,
                "agents": ",".join(sorted(str(a.spec.id) for a in agent_registry.list_agents())),
            },
        )
        try:
            self._recovery.recover(now)
        except Exception as exc:  # recovery failure must not leave a half-started worker
            self._workers.set_status(self.worker_id, WorkerStatus.ERROR, self._clock(), error="Startup recovery failed")
            raise RuntimeError(f"Worker startup recovery failed: {exc}") from exc

        self._hb_stop.clear()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name=f"baby-heartbeat-{self.worker_id[-8:]}", daemon=True
        )
        self._hb_thread.start()
        self._workers.set_status(self.worker_id, WorkerStatus.IDLE, self._clock())
        self._started = True
        metrics_collector.increment_counter("worker_started_total")
        audit_log.record(
            AuditEventType.WORKER_STARTED,
            details={"worker_id": self.worker_id, "pid": os.getpid(), "slot": self.config.slot},
        )
        logger.info("Worker started", worker_id=self.worker_id, pid=os.getpid())

    def shutdown(self) -> None:
        """Stop heartbeating and record the final worker state."""
        if not self._started:
            return
        self._started = False
        self._hb_stop.set()
        if self._hb_thread is not None:
            self._hb_thread.join(timeout=max(1.0, self.config.heartbeat_interval * 2))
        final = WorkerStatus.ERROR if self._fatal else WorkerStatus.STOPPED
        try:
            self._workers.set_status(self.worker_id, final, self._clock(), error=self._fatal)
        except Exception as exc:
            logger.error("Could not record worker stop", worker_id=self.worker_id, error=str(exc))
        metrics_collector.increment_counter("worker_stopped_total", labels={"status": final.value})
        try:
            audit_log.record(
                AuditEventType.WORKER_STOPPED,
                details={
                    "worker_id": self.worker_id,
                    "status": final.value,
                    "tasks_processed": self.tasks_processed,
                    "reason": self._fatal or "graceful shutdown",
                },
            )
        except Exception as exc:
            logger.error("Could not audit worker stop", worker_id=self.worker_id, error=str(exc))
        logger.info("Worker stopped", worker_id=self.worker_id, status=final.value)

    async def run(self) -> int:
        """Run until stopped. Returns a process exit code (0 graceful, 1 fatal)."""
        self.startup()
        try:
            last_recovery = self._clock()
            while not self.stop_requested():
                if self.config.max_tasks is not None and self.tasks_processed >= self.config.max_tasks:
                    break
                claimed = self._queue.claim_next(self.worker_id)
                if claimed is not None:
                    await self.execute_claimed(claimed)
                    continue
                if self._clock() - last_recovery >= timedelta(seconds=self.config.stale_timeout):
                    self._recovery.recover()
                    last_recovery = self._clock()
                await self._sleep(self.config.poll_interval)
        finally:
            self.shutdown()
        return 1 if self._fatal else 0

    async def _sleep(self, seconds: float) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while not self.stop_requested():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, 0.05))

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def heartbeat_once(self) -> bool:
        """Write one heartbeat. Returns False if the identity is revoked or the write failed."""
        try:
            alive = self._workers.heartbeat(self.worker_id, self._clock())
        except Exception as exc:
            self._on_heartbeat_failure(f"heartbeat write failed: {exc.__class__.__name__}")
            return False
        if not alive:
            self._on_heartbeat_failure("worker identity no longer live (marked stale/stopped by recovery)")
            self._fatal = "Worker identity revoked; heartbeat rejected"
        return alive

    def _on_heartbeat_failure(self, reason: str) -> None:
        metrics_collector.increment_counter("worker_heartbeat_failures_total")
        logger.warning("Worker heartbeat failed", worker_id=self.worker_id, reason=reason)
        try:
            audit_log.record(
                AuditEventType.WORKER_HEARTBEAT_FAILED,
                details={"worker_id": self.worker_id, "reason": reason},
            )
        except Exception:
            pass  # the audit store may be the thing that is failing

    def _heartbeat_loop(self) -> None:
        failures = 0
        max_failures = max(1, int(self.config.stale_timeout / self.config.heartbeat_interval) - 1)
        while not self._hb_stop.wait(self.config.heartbeat_interval):
            if self.heartbeat_once():
                failures = 0
                continue
            if self._fatal:
                return
            failures += 1
            if failures >= max_failures:
                self._fatal = f"Heartbeat failed {failures} consecutive times"
                return

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------

    def _audit(self, event: AuditEventType, claimed: ClaimedTask, **details: Any) -> None:
        audit_log.record(
            event,
            task_id=claimed.record.task_id,
            user_id=claimed.record.user_id,
            details={"worker_id": self.worker_id, "fence": claimed.lease.fence, **details},
        )

    async def execute_claimed(self, claimed: ClaimedTask) -> Optional[TaskStatus]:
        """Execute one claimed task under lease supervision. Returns the status written, if any."""
        lease = claimed.lease
        task_id = claimed.record.task_id
        self._audit(AuditEventType.TASK_CLAIMED, claimed, resumed_after_approval=claimed.resumed_after_approval)
        logger.info("Task claimed", worker_id=self.worker_id, task_id=str(task_id), fence=lease.fence)

        started = self._clock()
        run = asyncio.ensure_future(self._executor(claimed.to_task()))
        stop_status: Optional[TaskStatus] = None
        stop_error: Optional[str] = None
        try:
            while True:
                done, _ = await asyncio.wait({run}, timeout=self.config.heartbeat_interval)
                if run in done:
                    break
                try:
                    state = self._queue.renew(task_id, self.worker_id, lease.fence)
                except Exception as exc:
                    logger.error("Lease renewal failed", task_id=str(task_id), error=str(exc))
                    state = LeaseState(held=False)
                if not state.held:
                    await self._cancel(run)
                    self._audit(AuditEventType.TASK_LEASE_LOST, claimed, action="execution stopped; no result written")
                    metrics_collector.increment_counter("worker_task_leases_lost_total")
                    self._mark_idle()
                    self.tasks_processed += 1
                    return None
                self._audit(AuditEventType.TASK_LEASE_RENEWED, claimed)
                if state.cancel_requested:
                    stop_status, stop_error = TaskStatus.CANCELLED, "Cancelled while in progress"
                elif self._clock() - started > timedelta(seconds=self.config.max_task_seconds):
                    stop_status = TaskStatus.FAILED
                    stop_error = f"Task exceeded worker_max_task_seconds ({self.config.max_task_seconds}s)"
                elif self.stop_requested():
                    stop_status = TaskStatus.FAILED
                    stop_error = (
                        f"Worker '{self.worker_id}' shut down before the task finished; "
                        "interrupted work was not replayed"
                    )
                if stop_status is not None:
                    await self._cancel(run)
                    break
        finally:
            if not run.done():
                await self._cancel(run)

        if stop_status is not None:
            return self._write(claimed, stop_status, error=stop_error)

        try:
            result = run.result()
        except asyncio.CancelledError:
            return self._write(claimed, TaskStatus.FAILED, error="Task execution was cancelled")
        except Exception as exc:
            return self._write(claimed, TaskStatus.FAILED, error=f"Task execution error: {exc}")

        if result is None or result.success:
            return self._write(claimed, TaskStatus.COMPLETED, result=result)

        from baby.configuration import settings
        from baby.permissions.approvals import approval_service

        if approval_service.has_pending(task_id):
            # Each resume re-runs the task from the start, so an unbounded series of approval
            # pauses could keep asking an admin to re-approve; cap it and fail closed.
            rounds = approval_service.approval_rounds(task_id)
            if rounds > settings.max_approval_rounds:
                return self._write(
                    claimed,
                    TaskStatus.FAILED,
                    result=result,
                    error=(
                        f"Task needed approval {rounds} times, more than max_approval_rounds "
                        f"({settings.max_approval_rounds}); failed closed instead of pausing again"
                    ),
                )
            return self._write(claimed, TaskStatus.AWAITING_APPROVAL, result=result)
        return self._write(claimed, TaskStatus.FAILED, result=result, error=result.error)

    @staticmethod
    async def _cancel(run: "asyncio.Future[Any]") -> None:
        run.cancel()
        try:
            await run
        except BaseException:
            pass

    def _mark_idle(self) -> None:
        try:
            self._workers.set_status(self.worker_id, WorkerStatus.IDLE, self._clock())
        except Exception as exc:
            logger.warning("Could not mark worker idle", worker_id=self.worker_id, error=str(exc))

    def _write(
        self,
        claimed: ClaimedTask,
        status: TaskStatus,
        result: Optional[AgentResult] = None,
        error: Optional[str] = None,
    ) -> Optional[TaskStatus]:
        task_id, lease = claimed.record.task_id, claimed.lease
        self.tasks_processed += 1
        try:
            if status == TaskStatus.AWAITING_APPROVAL:
                self._queue.park_for_approval(task_id, self.worker_id, lease.fence, result=result)
            else:
                self._queue.finalize(task_id, self.worker_id, lease.fence, status, result=result, error=error)
        except LeaseLostError as exc:
            self._audit(AuditEventType.TASK_LEASE_LOST, claimed, action="result discarded", error=str(exc))
            metrics_collector.increment_counter("worker_task_leases_lost_total")
            self._mark_idle()
            return None
        except Exception as exc:
            logger.error("Could not record task outcome", task_id=str(task_id), error=str(exc))
            self._mark_idle()
            return None

        event = {
            TaskStatus.COMPLETED: AuditEventType.TASK_COMPLETED,
            TaskStatus.FAILED: AuditEventType.TASK_FAILED,
            TaskStatus.CANCELLED: AuditEventType.TASK_CANCELLED,
            TaskStatus.AWAITING_APPROVAL: AuditEventType.APPROVAL_REQUESTED,
        }[status]
        self._audit(event, claimed, status=status.value, error=error)
        logger.info("Task outcome recorded", task_id=str(task_id), status=status.value, worker_id=self.worker_id)
        return status
