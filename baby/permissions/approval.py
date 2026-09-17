"""Approval tracking for high-risk actions."""

from typing import Dict, Optional
from uuid import UUID

from baby.audit import audit_log
from baby.core import AgentId, ApprovalRequest, AuditEventType, TaskId, utc_now


class ApprovalManager:
    """Track explicit approval decisions for risky actions."""

    def __init__(self) -> None:
        self._requests: Dict[UUID, ApprovalRequest] = {}

    def request_approval(
        self,
        *,
        task_id: TaskId,
        agent_id: AgentId,
        action_type: str,
        reason: str,
        risk_level: str,
        user_id: Optional[str] = None,
    ) -> ApprovalRequest:
        """Create and record an approval request."""
        existing = self.find_request(task_id=task_id, agent_id=agent_id, action_type=action_type)
        if existing is not None and existing.approved is None:
            return existing

        request = ApprovalRequest(
            task_id=task_id,
            agent_id=agent_id,
            action_type=action_type,
            reason=reason,
            risk_level=risk_level,
        )
        self._requests[request.request_id] = request
        audit_log.record(
            AuditEventType.APPROVAL_REQUESTED,
            task_id=task_id,
            agent_id=agent_id,
            user_id=user_id,
            details={"request_id": str(request.request_id), "action_type": action_type, "risk_level": risk_level},
        )
        return request

    def decide(self, request_id: UUID, approved: bool, user_id: Optional[str] = None) -> ApprovalRequest:
        """Record an explicit approval decision."""
        request = self._requests[request_id]
        updated = request.model_copy(
            update={
                "approved": approved,
                "approval_timestamp": request.approval_timestamp or utc_now(),
            }
        )
        self._requests[request_id] = updated
        audit_log.record(
            AuditEventType.APPROVAL_GRANTED if approved else AuditEventType.APPROVAL_DENIED,
            task_id=updated.task_id,
            agent_id=updated.agent_id,
            user_id=user_id,
            details={"request_id": str(updated.request_id), "action_type": updated.action_type},
        )
        return updated

    def find_request(self, *, task_id: TaskId, agent_id: AgentId, action_type: str) -> Optional[ApprovalRequest]:
        """Return the latest request matching the action if present."""
        matches = [
            request
            for request in self._requests.values()
            if request.task_id == task_id and request.agent_id == agent_id and request.action_type == action_type
        ]
        if not matches:
            return None
        matches.sort(key=lambda request: request.created_at, reverse=True)
        return matches[0]

    def clear(self) -> None:
        """Clear all requests; intended for tests."""
        self._requests.clear()


approval_manager = ApprovalManager()
