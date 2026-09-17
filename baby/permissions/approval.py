"""Approval workflow management for high-risk actions."""

import hashlib
import json

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
        action_fingerprint: str,
        user_id: str | None = None,
    ) -> ApprovalRequest:
        """Create and audit a new approval request."""
        for existing_request in self._requests.values():
            if (
                existing_request.task_id == task_id
                and existing_request.agent_id == agent_id
                and existing_request.action_type == action_type
                and existing_request.action_fingerprint == action_fingerprint
                and existing_request.approved is None
            ):
                return existing_request

        request = ApprovalRequest(
            task_id=task_id,
            agent_id=agent_id,
            action_type=action_type,
            reason=reason,
            risk_level=risk_level,
            action_fingerprint=action_fingerprint,
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
                "action_fingerprint": action_fingerprint,
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
        action_fingerprint: str,
    ) -> ApprovalRequest:
        """Require a matching, explicitly approved request."""
        request = self.get_request(request_id)
        if (
            request.task_id != task_id
            or request.agent_id != agent_id
            or request.action_type != action_type
            or request.action_fingerprint != action_fingerprint
        ):
            raise ApprovalDeniedError("Approval request does not match the requested action")
        if request.approved is True:
            return request
        if request.approved is False:
            raise ApprovalDeniedError(f"Approval denied for {action_type}")
        raise ApprovalRequiredError(f"Approval required for {action_type}: request_id={request_id}")

    def clear(self) -> None:
        """Clear requests for tests."""
        self._requests.clear()

    @staticmethod
    def fingerprint_action(payload: dict[str, object]) -> str:
        """Return a deterministic fingerprint for approval-scoped action input."""

        def normalize(value: object) -> object:
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            if isinstance(value, dict):
                return {str(key): normalize(nested_value) for key, nested_value in sorted(value.items())}
            if isinstance(value, (list, tuple)):
                return [normalize(item) for item in value]
            if isinstance(value, set):
                normalized_items = [normalize(item) for item in value]
                return sorted(normalized_items, key=lambda item: json.dumps(item, sort_keys=True))
            model_dump = getattr(value, "model_dump", None)
            if callable(model_dump):
                return normalize(model_dump(mode="json"))
            return str(value)

        normalized = normalize(payload)
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


approval_manager = ApprovalManager()
