"""Task visibility and lifecycle tracking for BABY.

Maintains operational state for tasks, including execution status, plan
steps, assigned agents/workers, approvals, verifications, and errors.

Phase 10 & 11 additions:
- Enforced valid state transitions (invalid transitions raise InvalidStateTransitionError)
- Terminal state protection (COMPLETED/FAILED/CANCELLED cannot transition)
- Restart recovery: recover_stale_tasks() marks IN_PROGRESS tasks as FAILED
  on startup to ensure fail-closed behavior after unexpected process termination
- Durable SQLite task store integration preserving state across restarts
"""

import threading
from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID

from baby.configuration import settings
from baby.core import (
    VALID_TASK_TRANSITIONS,
    AgentId,
    AgentResult,
    ApprovalRequest,
    Plan,
    Task,
    TaskId,
    TaskRecord,
    TaskStatus,
    VerificationResult,
)
from baby.errors import InvalidStateTransitionError
from baby.logging import get_logger
from baby.observability.metrics import metrics_collector
from baby.persistence.sqlite_tasks import SQLiteTaskStore

logger = get_logger(__name__)

# States that cannot accept further transitions (terminal states)
_TERMINAL_STATES = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}


class TaskTracker:
    """Thread-safe task tracker with state-machine enforcement and SQLite persistence."""

    def __init__(self, task_store: Optional[SQLiteTaskStore] = None) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[str, TaskRecord] = {}

        # Initialize durable SQLite backing if enabled
        if task_store is not None:
            self._store: Optional[SQLiteTaskStore] = task_store
        elif getattr(settings, "persistence_backend", "sqlite") == "sqlite":
            try:
                self._store = SQLiteTaskStore()
            except Exception as exc:
                logger.warning("Could not initialize SQLite task store; running in memory", error=str(exc))
                self._store = None
        else:
            self._store = None

    def register_task(
        self,
        task: Task,
        worker_id: Optional[str] = None,
        status: TaskStatus = TaskStatus.PENDING,
    ) -> TaskRecord:
        """Register a new task in the tracker and persist to durable store."""
        with self._lock:
            key = str(task.id)
            now = datetime.utcnow()
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
            self._tasks[key] = record
            if self._store:
                try:
                    self._store.save_task(record)
                except Exception as exc:
                    logger.error("Failed to persist registered task to SQLite", error=str(exc), task_id=key)
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
        with self._lock:
            key = str(task_id)
            if key not in self._tasks:
                # Attempt to load from store if missing from cache
                if self._store:
                    persisted = self._store.get_task(task_id)
                    if persisted:
                        self._tasks[key] = persisted
                    else:
                        return None
                else:
                    return None

            record = self._tasks[key]
            current = record.status

            # Enforce state machine: reject invalid transitions
            allowed = VALID_TASK_TRANSITIONS.get(current, set())
            if status not in allowed:
                if current in _TERMINAL_STATES:
                    raise InvalidStateTransitionError(
                        f"Task {task_id} is in terminal state '{current}' and cannot transition to '{status}'"
                    )
                raise InvalidStateTransitionError(
                    f"Invalid task state transition: '{current}' → '{status}'. "
                    f"Allowed transitions from '{current}': {sorted(str(s) for s in allowed) or 'none (terminal)'}"
                )

            record.status = status
            record.updated_at = datetime.utcnow()

            if error is not None:
                record.error = error
            if result is not None:
                record.result = result
            if agent_id is not None:
                record.assigned_agent_id = agent_id

            if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                record.completed_at = record.updated_at
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

            # Persist updated status
            if self._store:
                try:
                    self._store.save_task(record)
                except Exception as exc:
                    logger.error("Failed to persist task status update to SQLite", error=str(exc), task_id=key)

            return record

    def recover_stale_tasks(self, stale_error: str = "Recovered after unexpected process restart") -> List[TaskRecord]:
        """Mark all IN_PROGRESS tasks as FAILED for fail-closed restart recovery.

        Must be called at application startup before accepting new work.
        Inspects both SQLite storage and in-memory cache to guarantee that
        tasks interrupted across process restarts are cleanly transitioned.

        Returns:
            List of TaskRecords that were transitioned to FAILED.
        """
        recovered = []
        with self._lock:
            # If persistent store exists, sync all tasks from database into cache
            if self._store:
                try:
                    persisted_tasks = self._store.list_tasks(limit=10000)
                    for pt in persisted_tasks:
                        if str(pt.task_id) not in self._tasks:
                            self._tasks[str(pt.task_id)] = pt
                except Exception as exc:
                    logger.warning("Could not sync tasks from store during recovery", error=str(exc))

            for record in self._tasks.values():
                if record.status == TaskStatus.IN_PROGRESS:
                    record.status = TaskStatus.FAILED
                    record.error = stale_error
                    record.updated_at = datetime.utcnow()
                    record.completed_at = record.updated_at
                    duration_ms = (record.completed_at - record.created_at).total_seconds() * 1000
                    metrics_collector.record_task_failed(duration_ms, error_type="stale_restart")

                    if self._store:
                        try:
                            self._store.save_task(record)
                        except Exception as exc:
                            logger.error("Failed to persist recovered task status", error=str(exc))

                    recovered.append(record)
                    logger.warning(
                        "Stale in-progress task recovered as failed",
                        task_id=str(record.task_id),
                        title=record.title,
                    )
        return recovered

    def set_plan(self, task_id: TaskId, plan: Plan) -> Optional[TaskRecord]:
        """Attach plan to a task."""
        with self._lock:
            key = str(task_id)
            if key not in self._tasks:
                return None
            record = self._tasks[key]
            record.plan = plan
            record.updated_at = datetime.utcnow()
            if self._store:
                try:
                    self._store.save_task(record)
                except Exception as exc:
                    logger.error("Failed to persist plan to SQLite", error=str(exc))
            return record

    def record_approval_request(self, task_id: TaskId, approval: ApprovalRequest) -> Optional[TaskRecord]:
        """Record an approval request on a task."""
        with self._lock:
            key = str(task_id)
            if key not in self._tasks:
                return None
            record = self._tasks[key]
            record.approvals.append(approval)
            record.status = TaskStatus.AWAITING_APPROVAL
            record.updated_at = datetime.utcnow()
            metrics_collector.record_approval_requested()
            if self._store:
                try:
                    self._store.save_task(record)
                except Exception as exc:
                    logger.error("Failed to persist approval request to SQLite", error=str(exc))
            return record

    def record_approval_decision(self, task_id: TaskId, request_id: UUID, approved: bool) -> Optional[TaskRecord]:
        """Record the resolution of an approval request."""
        with self._lock:
            key = str(task_id)
            if key not in self._tasks:
                return None
            record = self._tasks[key]
            now = datetime.utcnow()
            for req in record.approvals:
                if req.request_id == request_id:
                    req.approved = approved
                    req.approval_timestamp = now
            record.updated_at = now
            metrics_collector.record_approval_decision(approved)
            if self._store:
                try:
                    self._store.save_task(record)
                except Exception as exc:
                    logger.error("Failed to persist approval decision to SQLite", error=str(exc))
            return record

    def record_verification(self, task_id: TaskId, verification: VerificationResult) -> Optional[TaskRecord]:
        """Record verification outcome on a task."""
        with self._lock:
            key = str(task_id)
            if key not in self._tasks:
                return None
            record = self._tasks[key]
            record.verifications.append(verification)
            record.updated_at = datetime.utcnow()
            metrics_collector.record_verification(verification.verified)
            if self._store:
                try:
                    self._store.save_task(record)
                except Exception as exc:
                    logger.error("Failed to persist verification to SQLite", error=str(exc))
            return record

    def get_task(self, task_id: TaskId) -> Optional[TaskRecord]:
        """Retrieve task record by ID."""
        with self._lock:
            record = self._tasks.get(str(task_id))
            if record is None and self._store:
                record = self._store.get_task(task_id)
                if record:
                    self._tasks[str(task_id)] = record
            return record

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
