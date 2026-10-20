"""Task visibility and lifecycle tracking for BABY.

Maintains operational state for tasks, including execution status, plan
steps, assigned agents/workers, approvals, verifications, and errors.

Phase 10 & 11 additions:
- Enforced valid state transitions (invalid transitions raise InvalidStateTransitionError)
- Terminal state protection (COMPLETED/FAILED/CANCELLED cannot transition)
- Restart recovery: recover_stale_tasks() marks IN_PROGRESS tasks as FAILED
  on startup to ensure fail-closed behavior after unexpected process termination
- Durable SQLite task store integration preserving state across restarts

Phase 12: when a SQLite store is configured the database is authoritative. Every read
goes to the store and every mutation is an atomic read-modify-write
(``SQLiteTaskStore.update``), so the API process always observes status changes made by
separate worker processes and two processes can never apply conflicting transitions.
The in-memory dictionary is used only for ``persistence_backend='in_memory'``.
"""

import threading
from typing import Callable, Dict, List, Optional, Tuple
from uuid import UUID

from baby.configuration import settings
from baby.core import (
    TERMINAL_TASK_STATES,
    AgentId,
    AgentResult,
    ApprovalRequest,
    Plan,
    Task,
    TaskId,
    TaskRecord,
    TaskStatus,
    VerificationResult,
    ensure_valid_transition,
    utc_now,
)
from baby.logging import get_logger
from baby.observability.metrics import metrics_collector
from baby.persistence.sqlite_tasks import SQLiteTaskStore

logger = get_logger(__name__)

# States that cannot accept further transitions (terminal states)
_TERMINAL_STATES = TERMINAL_TASK_STATES


class TaskTracker:
    """Thread-safe task tracker with state-machine enforcement and SQLite persistence."""

    def __init__(self, task_store: Optional[SQLiteTaskStore] = None) -> None:
        self._lock = threading.Lock()
        self._store_init_lock = threading.Lock()
        self._tasks: Dict[str, TaskRecord] = {}

        # The backend is chosen now, but the default SQLite store is opened on first use so
        # that importing Baby (e.g. ``python -m baby.workers --help``) never creates a database.
        self._store_instance: Optional[SQLiteTaskStore] = task_store
        self._store_pending = task_store is None and getattr(settings, "persistence_backend", "sqlite") == "sqlite"

    @property
    def _store(self) -> Optional[SQLiteTaskStore]:
        if self._store_pending:
            with self._store_init_lock:
                if self._store_pending:
                    try:
                        self._store_instance = SQLiteTaskStore()
                    except Exception as exc:
                        logger.warning("Could not initialize SQLite task store; running in memory", error=str(exc))
                        self._store_instance = None
                    self._store_pending = False
        return self._store_instance

    @property
    def store(self) -> Optional[SQLiteTaskStore]:
        """The durable store, or None when running purely in memory."""
        return self._store

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _mutate(self, task_id: TaskId, mutate: Callable[[TaskRecord], None]) -> Optional[TaskRecord]:
        """Apply ``mutate`` atomically to the authoritative copy of a task."""
        if self._store is not None:
            return self._store.update(task_id, mutate)
        with self._lock:
            record = self._tasks.get(str(task_id))
            if record is None:
                return None
            mutate(record)
            return record

    @staticmethod
    def _apply_transition(
        record: TaskRecord,
        status: TaskStatus,
        error: Optional[str],
        result: Optional[AgentResult],
        agent_id: Optional[AgentId],
    ) -> None:
        ensure_valid_transition(record.task_id, record.status, status)
        record.status = status
        record.updated_at = utc_now()
        if error is not None:
            record.error = error
        if result is not None:
            record.result = result
        if agent_id is not None:
            record.assigned_agent_id = agent_id
        if status in _TERMINAL_STATES:
            record.completed_at = record.updated_at

    @staticmethod
    def _record_transition_metrics(record: TaskRecord, error: Optional[str]) -> None:
        status = record.status
        if status in _TERMINAL_STATES and record.completed_at is not None:
            duration_ms = (record.completed_at - record.created_at).total_seconds() * 1000
            if status == TaskStatus.COMPLETED:
                metrics_collector.record_task_completed(duration_ms)
            elif status == TaskStatus.FAILED:
                metrics_collector.record_task_failed(duration_ms, error_type=error or "generic")
            else:
                metrics_collector.record_task_failed(duration_ms, error_type="cancelled")
        elif status == TaskStatus.IN_PROGRESS:
            metrics_collector.record_task_started()
        elif status == TaskStatus.AWAITING_APPROVAL:
            metrics_collector.record_approval_requested()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_task(
        self,
        task: Task,
        worker_id: Optional[str] = None,
        status: TaskStatus = TaskStatus.PENDING,
    ) -> TaskRecord:
        """Register a new task in the tracker and persist to durable store.

        With a durable store configured, a persistence failure raises (fail closed):
        a task that cannot be durably recorded must not be reported as accepted.
        """
        now = utc_now()
        record = TaskRecord(
            task_id=task.id,
            title=task.title,
            description=task.description or "",
            status=status,
            created_at=task.created_at or now,
            updated_at=now,
            user_id=task.user_id,
            priority=task.priority,
            assigned_worker_id=worker_id,
        )
        if self._store is not None:
            self._store.save_task(record)
        else:
            with self._lock:
                self._tasks[str(task.id)] = record
        metrics_collector.record_task_created(task.priority)
        return record

    def update_status(
        self,
        task_id: TaskId,
        status: TaskStatus,
        error: Optional[str] = None,
        result: Optional[AgentResult] = None,
        agent_id: Optional[AgentId] = None,
    ) -> Optional[TaskRecord]:
        """Update task execution status, enforcing valid state transitions.

        Raises:
            InvalidStateTransitionError: If the transition from current to target
                state is not permitted by the task state machine. Terminal states
                (COMPLETED, FAILED, CANCELLED) cannot transition further.
        """
        record = self._mutate(task_id, lambda r: self._apply_transition(r, status, error, result, agent_id))
        if record is not None:
            self._record_transition_metrics(record, error)
        return record

    def recover_stale_tasks(self, stale_error: str = "Recovered after unexpected process restart") -> List[TaskRecord]:
        """Mark orphaned IN_PROGRESS tasks as FAILED for fail-closed restart recovery.

        Must be called at application startup before accepting new work.

        A task that is IN_PROGRESS *and* holds a durable worker lease belongs to a worker
        process that may still be alive (for example when only the API restarted); it is
        left to lease-based recovery (``baby.workers.recovery``), which fails it closed
        only once the lease expires or its worker is detected as stale. Every other
        IN_PROGRESS task has no live owner and is failed closed here — never replayed.

        Returns:
            List of TaskRecords that were transitioned to FAILED.
        """
        recovered: List[TaskRecord] = []

        def _fail(record: TaskRecord) -> None:
            ensure_valid_transition(record.task_id, record.status, TaskStatus.FAILED)
            record.status = TaskStatus.FAILED
            record.error = stale_error
            record.updated_at = utc_now()
            record.completed_at = record.updated_at

        if self._store is not None:
            store = self._store
            candidates = store.list_tasks(status=TaskStatus.IN_PROGRESS, limit=-1)
            for candidate in candidates:
                with store.db.immediate_transaction() as conn:
                    record = store.read_record(conn, candidate.task_id)
                    if record is None or record.status != TaskStatus.IN_PROGRESS:
                        continue
                    if store.has_lease(conn, record.task_id):
                        continue
                    _fail(record)
                    store.write_record(conn, record)
                recovered.append(record)
        else:
            with self._lock:
                for record in self._tasks.values():
                    if record.status == TaskStatus.IN_PROGRESS:
                        _fail(record)
                        recovered.append(record)

        for record in recovered:
            duration_ms = (record.updated_at - record.created_at).total_seconds() * 1000
            metrics_collector.record_task_failed(duration_ms, error_type="stale_restart")
            logger.warning(
                "Stale in-progress task recovered as failed",
                task_id=str(record.task_id),
                title=record.title,
            )
        return recovered

    def set_plan(self, task_id: TaskId, plan: Plan) -> Optional[TaskRecord]:
        """Attach plan to a task."""

        def _set(record: TaskRecord) -> None:
            record.plan = plan
            record.updated_at = utc_now()

        return self._mutate(task_id, _set)

    def set_plan_if_absent(self, task_id: TaskId, plan: Plan) -> Tuple[Optional[Plan], bool]:
        """Attach ``plan`` unless the task already has one, atomically; return the stored plan.

        A task's plan is fixed the first time it is stored: a resumed task (or a racing worker)
        gets the original plan back, so its steps and approval digests never change.

        Returns:
            ``(stored_plan, created)``: the plan now stored on the task (None if the task does not
            exist) and whether ``plan`` was the one stored by this call.
        """
        created = False

        def _set(record: TaskRecord) -> None:
            nonlocal created
            if record.plan is None:
                record.plan = plan
                record.updated_at = utc_now()
                created = True

        record = self._mutate(task_id, _set)
        return (record.plan if record else None), created

    def record_approval_request(self, task_id: TaskId, approval: ApprovalRequest) -> Optional[TaskRecord]:
        """Record an approval request on a task and move it to AWAITING_APPROVAL.

        The transition is validated by the task state machine (only IN_PROGRESS tasks
        may start waiting for approval).
        """

        def _request(record: TaskRecord) -> None:
            if record.status != TaskStatus.AWAITING_APPROVAL:
                ensure_valid_transition(record.task_id, record.status, TaskStatus.AWAITING_APPROVAL)
            record.approvals.append(approval)
            record.status = TaskStatus.AWAITING_APPROVAL
            record.updated_at = utc_now()

        record = self._mutate(task_id, _request)
        if record is not None:
            metrics_collector.record_approval_requested()
        return record

    def record_approval_decision(self, task_id: TaskId, request_id: UUID, approved: bool) -> Optional[TaskRecord]:
        """Record the resolution of an approval request."""

        def _decide(record: TaskRecord) -> None:
            now = utc_now()
            for req in record.approvals:
                if req.request_id == request_id:
                    req.approved = approved
                    req.approval_timestamp = now
            record.updated_at = now

        record = self._mutate(task_id, _decide)
        if record is not None:
            metrics_collector.record_approval_decision(approved)
        return record

    def record_verification(self, task_id: TaskId, verification: VerificationResult) -> Optional[TaskRecord]:
        """Record verification outcome on a task."""

        def _verify(record: TaskRecord) -> None:
            record.verifications.append(verification)
            record.updated_at = utc_now()

        record = self._mutate(task_id, _verify)
        if record is not None:
            metrics_collector.record_verification(verification.verified)
        return record

    def get_task(self, task_id: TaskId) -> Optional[TaskRecord]:
        """Retrieve task record by ID (always the authoritative copy)."""
        if self._store is not None:
            return self._store.get_task(task_id)
        with self._lock:
            return self._tasks.get(str(task_id))

    def _filter_tasks(
        self,
        user_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
    ) -> List[TaskRecord]:
        tasks = list(self._tasks.values())
        if user_id is not None:
            tasks = [t for t in tasks if t.user_id == user_id]
        if status is not None:
            tasks = [t for t in tasks if t.status == status]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks

    def list_tasks(
        self,
        user_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[TaskRecord]:
        """List tasks with optional engagement and status filters and pagination."""
        if self._store is not None:
            return self._store.list_tasks(user_id=user_id, status=status, limit=limit, offset=offset)
        with self._lock:
            matched = self._filter_tasks(user_id=user_id, status=status)
            if offset > 0:
                matched = matched[offset:]
            if limit >= 0:
                matched = matched[:limit]
            return matched

    def count_tasks(
        self,
        user_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
    ) -> int:
        """Count tasks matching the filters."""
        if self._store is not None:
            return self._store.count_tasks(user_id=user_id, status=status)
        with self._lock:
            return len(self._filter_tasks(user_id=user_id, status=status))

    def clear(self) -> None:
        """Clear all tasks; intended for testing."""
        with self._lock:
            self._tasks.clear()
        if self._store:
            try:
                self._store.clear()
            except Exception as exc:
                logger.warning("Error clearing SQLite task store", error=str(exc))


# Global task tracker instance
task_tracker = TaskTracker()
