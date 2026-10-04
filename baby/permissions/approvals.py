"""Durable, operation-bound approval grants.

Security model:
- An approval request is created only by the executor/orchestrator when an operation
  needs approval. It records the task, engagement, agent, permission and a SHA-256
  ``operation_digest`` of the exact operation (name + agent + permission + arguments).
- Only ``ApprovalService.decide`` can approve or deny a request. It is called from the
  admin-only API endpoint. No worker code path calls it, and worker identity or task
  ownership is never accepted as authority to approve.
- A granted approval authorizes exactly one execution of exactly that operation, in that
  task, for that engagement, while the task is IN_PROGRESS. It is consumed atomically
  (``consumed_at``) so a crash can never cause it to be replayed.
- Approval state lives in the durable task record, so it is shared between the API and
  worker processes and survives restarts.
"""

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional
from uuid import UUID

from baby.core import AgentId, ApprovalRequest, TaskId, TaskRecord, TaskStatus, ensure_valid_transition
from baby.errors import InvalidStateTransitionError, TaskNotFoundError
from baby.logging import get_logger

if TYPE_CHECKING:
    from baby.persistence.sqlite_tasks import SQLiteTaskStore

logger = get_logger(__name__)

_SUMMARY_MAX_CHARS = 500


def operation_digest(operation: str, agent_id: str, permission: str, arguments: Dict[str, Any]) -> str:
    """Stable digest binding an approval to one exact operation."""
    canonical = json.dumps(
        {"operation": operation, "agent_id": agent_id, "permission": permission, "arguments": arguments},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def summarize_arguments(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Human-reviewable, redacted, size-bounded summary of operation arguments."""
    from baby.observability.redaction import redact_sensitive_data

    summary: Dict[str, Any] = {}
    for key, value in arguments.items():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if len(text) > _SUMMARY_MAX_CHARS:
            text = text[:_SUMMARY_MAX_CHARS] + f"... [{len(text)} chars total]"
        summary[key] = text
    redacted = redact_sensitive_data(summary)
    return redacted if isinstance(redacted, dict) else {}


class ApprovalService:
    """Creates, decides and consumes approval requests stored on durable task records."""

    def _store(self) -> Optional["SQLiteTaskStore"]:
        from baby.observability.tasks import task_tracker

        return task_tracker.store

    def authorize_or_request(
        self,
        task_id: Optional[TaskId],
        agent_id: AgentId,
        engagement_id: Optional[str],
        operation: str,
        permission: str,
        arguments: Dict[str, Any],
        reason: str,
        risk_level: str = "high",
    ) -> bool:
        """Consume a matching granted approval, or record a pending request.

        Returns True only when an approved, unconsumed request for this exact task,
        engagement and operation exists and the task is IN_PROGRESS; that request is
        marked consumed in the same transaction. Otherwise returns False (and records a
        pending request if none exists yet) so the caller must block the operation.
        Without a durable task record nothing can be approved, so this fails closed.
        """
        store = self._store()
        if task_id is None or store is None:
            return False

        digest = operation_digest(operation, str(agent_id), permission, arguments)
        granted = False

        with store.db.immediate_transaction() as conn:
            record: Optional[TaskRecord] = store.read_record(conn, task_id)
            if record is None or record.user_id != engagement_id:
                return False

            for request in record.approvals:
                if request.operation_digest != digest or request.engagement_id != record.user_id:
                    continue
                if request.approved is True and request.consumed_at is None:
                    if record.status != TaskStatus.IN_PROGRESS:
                        return False
                    request.consumed_at = datetime.utcnow()
                    granted = True
                    break
                if request.approved is None:
                    # Already waiting for a decision on this exact operation.
                    return False

            if not granted:
                record.approvals.append(
                    ApprovalRequest(
                        task_id=task_id,
                        agent_id=agent_id,
                        action_type=operation,
                        reason=reason,
                        risk_level=risk_level,
                        engagement_id=record.user_id,
                        permission=permission,
                        operation_digest=digest,
                        operation_summary=summarize_arguments(arguments),
                    )
                )
            record.updated_at = datetime.utcnow()
            store.write_record(conn, record)

        logger.info(
            "Approval consumed" if granted else "Approval requested",
            task_id=str(task_id),
            operation=operation,
        )
        return granted

    def has_pending(self, task_id: TaskId) -> bool:
        """True if the task has at least one undecided approval request."""
        store = self._store()
        if store is None:
            return False
        record = store.get_task(task_id)
        return bool(record and any(a.approved is None for a in record.approvals))

    def decide(
        self,
        task_id: TaskId,
        request_id: UUID,
        approved: bool,
        decided_by: str,
        reason: Optional[str] = None,
    ) -> TaskRecord:
        """Record a human approval decision. Callers must already have verified admin authority.

        - The task must be AWAITING_APPROVAL and the request must belong to it and be undecided.
        - Approve: the task stays AWAITING_APPROVAL; once no undecided requests remain a worker
          may claim it (AWAITING_APPROVAL → IN_PROGRESS) and consume the grant exactly once.
        - Deny: the task moves AWAITING_APPROVAL → FAILED (fail closed, nothing is executed).

        Raises:
            TaskNotFoundError: Unknown task or request.
            InvalidStateTransitionError: Task not awaiting approval or request already decided.
        """
        store = self._store()
        if store is None:
            raise TaskNotFoundError("Approvals require durable task persistence")

        with store.db.immediate_transaction() as conn:
            record = store.read_record(conn, task_id)
            if record is None:
                raise TaskNotFoundError(f"Task {task_id} not found")
            request = next((a for a in record.approvals if a.request_id == request_id), None)
            if request is None or request.task_id != task_id:
                raise TaskNotFoundError(f"Approval request {request_id} not found for task {task_id}")
            if record.status != TaskStatus.AWAITING_APPROVAL:
                raise InvalidStateTransitionError(f"Task {task_id} is '{record.status.value}', not awaiting approval")
            if request.approved is not None:
                raise InvalidStateTransitionError(f"Approval request {request_id} was already decided")

            now = datetime.utcnow()
            request.approved = approved
            request.approval_timestamp = now
            request.decided_by = decided_by
            record.updated_at = now
            if not approved:
                ensure_valid_transition(task_id, record.status, TaskStatus.FAILED)
                record.status = TaskStatus.FAILED
                record.error = f"Approval denied for '{request.action_type}'" + (f": {reason}" if reason else "")
                record.completed_at = now
            store.write_record(conn, record)
        return record


approval_service = ApprovalService()
