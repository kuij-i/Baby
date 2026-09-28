"""Task visibility and lifecycle tracking for BABY.

Maintains operational state for tasks, including execution status, plan
steps, assigned agents/workers, approvals, verifications, and errors.
"""

import threading
from datetime import datetime
from typing import Dict, List, Optional
from uuid import UUID

from baby.core import (
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
from baby.observability.metrics import metrics_collector


class TaskTracker:
    """Thread-safe in-memory task tracker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: Dict[str, TaskRecord] = {}

    def register_task(
        self,
        task: Task,
        worker_id: Optional[str] = None,
        status: TaskStatus = TaskStatus.PENDING,
    ) -> TaskRecord:
        """Register a new task in the tracker."""
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
        """Update task execution status."""
        with self._lock:
            key = str(task_id)
            if key not in self._tasks:
                return None

            record = self._tasks[key]
            record.status = status
            record.updated_at = datetime.utcnow()

            if error is not None:
                record.error = error
            if result is not None:
                record.result = result
            if agent_id is not None:
                record.assigned_agent_id = agent_id

            if status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                record.completed_at = record.updated_at
                duration_ms = (record.completed_at - record.created_at).total_seconds() * 1000
                if status == TaskStatus.COMPLETED:
                    metrics_collector.record_task_completed(duration_ms)
                else:
                    metrics_collector.record_task_failed(duration_ms, error_type=error or "generic")
            elif status == TaskStatus.IN_PROGRESS:
                metrics_collector.record_task_started()
            elif status == TaskStatus.AWAITING_APPROVAL:
                metrics_collector.record_approval_requested()

            return record

    def set_plan(self, task_id: TaskId, plan: Plan) -> Optional[TaskRecord]:
        """Attach plan to a task."""
        with self._lock:
            key = str(task_id)
            if key not in self._tasks:
                return None
            record = self._tasks[key]
            record.plan = plan
            record.updated_at = datetime.utcnow()
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
            return record

    def get_task(self, task_id: TaskId) -> Optional[TaskRecord]:
        """Retrieve task record by ID."""
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


# Global task tracker instance
task_tracker = TaskTracker()
