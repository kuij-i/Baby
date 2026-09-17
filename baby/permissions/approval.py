"""Approval workflow management for high-risk actions."""

from baby.audit import audit_log
from baby.core import AgentId, ApprovalRequest, AuditEventType, TaskId
from baby.core.contracts import utc_now
from baby.errors import ApprovalDeniedError, ApprovalRequiredError


class ApprovalManager:
    """Stores approval requests and explicit approval decisions in memory."""

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}

    def request_approval(
        self,
        *,
        task_id: TaskId | None,
        agent_id: AgentId,
        action_type: str,
        reason: str,
        risk_level: str,
        user_id: str | None = None,
    ) -> ApprovalRequest:
        """Create and audit a new approval request."""
        for existing_request in self._requests.values():
            if (
                existing_request.task_id == task_id
                and existing_request.agent_id == agent_id
                and existing_request.action_type == action_type
                and existing_request.approved is None
            ):
                return existing_request

        request = ApprovalRequest(
            task_id=task_id,
            agent_id=agent_id,
            action_type=action_type,
            reason=reason,
            risk_level=risk_level,
        )
        self._requests[str(request.request_id)] = request
        audit_log.record(
            AuditEventType.APPROVAL_REQUESTED,
            task_id=task_id,
            agent_id=agent_id,
            user_id=user_id,
            details={
                "request_id": str(request.request_id),
                "action_type": action_type,
                "risk_level": risk_level,
                "reason": reason,
            },
        )
        return request

    def decide(
        self,
        request_id: str,
        *,
        approved: bool,
        user_id: str | None = None,
    ) -> ApprovalRequest:
        """Store and audit an explicit approval decision."""
        request = self.get_request(request_id)
        if request.approved is not None:
            raise ApprovalDeniedError(f"Approval decision already recorded for {request.action_type}")
        request.approved = approved
        request.approval_timestamp = utc_now()
        audit_log.record(
            AuditEventType.APPROVAL_GRANTED if approved else AuditEventType.APPROVAL_DENIED,
            task_id=request.task_id,
            agent_id=request.agent_id,
            user_id=user_id,
            details={
                "request_id": str(request.request_id),
                "action_type": request.action_type,
                "risk_level": request.risk_level,
            },
        )
        return request

    def get_request(self, request_id: str) -> ApprovalRequest:
        """Return a stored approval request."""
        if request_id not in self._requests:
            raise ApprovalDeniedError(f"Unknown approval request: {request_id}")
        return self._requests[request_id]

    def require_approved(
        self,
        request_id: str,
        *,
        task_id: TaskId | None,
        agent_id: AgentId,
        action_type: str,
    ) -> ApprovalRequest:
        """Require a matching, explicitly approved request."""
        request = self.get_request(request_id)
        if request.task_id != task_id or request.agent_id != agent_id or request.action_type != action_type:
            raise ApprovalDeniedError("Approval request does not match the requested action")
        if request.approved is True:
            return request
        if request.approved is False:
            raise ApprovalDeniedError(f"Approval denied for {action_type}")
        raise ApprovalRequiredError(f"Approval required for {action_type}: request_id={request_id}")

    def clear(self) -> None:
        """Clear requests for tests."""
        self._requests.clear()


approval_manager = ApprovalManager()
