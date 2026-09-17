"""Tests for permission system."""

import pytest

from baby.audit import audit_log
from baby.core import (
    AgentId,
    AuditEventType,
    PermissionCategory,
    PermissionLevel,
    TaskId,
    ToolPermission,
)
from baby.errors import PermissionDeniedError
from baby.permissions import approval_manager
from baby.permissions.manager import permission_manager


class TestPermissionManager:
    """Tests for PermissionManager."""

    def setup_method(self) -> None:
        """Clear permissions before each test."""
        permission_manager.clear()
        approval_manager.clear()
        audit_log.clear()

    def test_grant_permission(self) -> None:
        """Test granting a permission."""
        perm = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read-only access",
        )
        permission_manager.grant_permission("agent-1", perm)

        assert permission_manager.has_permission("agent-1", PermissionCategory.READ_ONLY)

    def test_revoke_permission(self) -> None:
        """Test revoking a permission."""
        perm = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read-only access",
        )
        permission_manager.grant_permission("agent-1", perm)
        permission_manager.revoke_permission("agent-1", PermissionCategory.READ_ONLY)

        assert not permission_manager.has_permission("agent-1", PermissionCategory.READ_ONLY)

    def test_get_permission(self) -> None:
        """Test getting a specific permission."""
        perm = ToolPermission(
            category=PermissionCategory.LOCAL_WRITE,
            level=PermissionLevel.APPROVAL_REQUIRED,
            description="Write with approval",
        )
        permission_manager.grant_permission("agent-1", perm)

        retrieved = permission_manager.get_permission("agent-1", PermissionCategory.LOCAL_WRITE)
        assert retrieved is not None
        assert retrieved.level == PermissionLevel.APPROVAL_REQUIRED

    def test_check_permission_allowed(self) -> None:
        """Test checking permission that is allowed."""
        perm = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read-only access",
        )
        permission_manager.grant_permission("agent-1", perm)

        result = permission_manager.check_permission(
            "agent-1",
            ToolPermission(
                category=PermissionCategory.READ_ONLY,
                level=PermissionLevel.ALLOW,
                description="Read-only access",
            ),
        )
        assert result == PermissionLevel.ALLOW

    def test_check_permission_denied(self) -> None:
        """Test checking permission that is denied."""
        with pytest.raises(PermissionDeniedError):
            permission_manager.check_permission(
                "agent-1",
                ToolPermission(
                    category=PermissionCategory.DESTRUCTIVE_ACTION,
                    level=PermissionLevel.ALLOW,
                    description="Destructive action",
                ),
            )

    def test_check_permission_approval_required(self) -> None:
        """Test checking permission that requires approval."""
        perm = ToolPermission(
            category=PermissionCategory.FINANCIAL_ACTION,
            level=PermissionLevel.APPROVAL_REQUIRED,
            description="Financial action requires approval",
        )
        permission_manager.grant_permission("agent-1", perm)

        result = permission_manager.check_permission(
            "agent-1",
            ToolPermission(
                category=PermissionCategory.FINANCIAL_ACTION,
                level=PermissionLevel.ALLOW,
                description="Financial action",
            ),
        )
        assert result == PermissionLevel.APPROVAL_REQUIRED

    def test_check_permission_explicit_denial(self) -> None:
        """Test checking a permission granted at DENY still blocks execution."""
        permission_manager.grant_permission(
            "agent-1",
            ToolPermission(
                category=PermissionCategory.LOCAL_WRITE,
                level=PermissionLevel.DENY,
                description="Writes are denied",
            ),
        )

        with pytest.raises(PermissionDeniedError):
            permission_manager.check_permission(
                "agent-1",
                ToolPermission(
                    category=PermissionCategory.LOCAL_WRITE,
                    level=PermissionLevel.ALLOW,
                    description="Write requested",
                ),
            )

    def test_get_agent_permissions(self) -> None:
        """Test getting all permissions for an agent."""
        perm1 = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read-only",
        )
        perm2 = ToolPermission(
            category=PermissionCategory.LOCAL_WRITE,
            level=PermissionLevel.ALLOW,
            description="Write",
        )
        permission_manager.grant_permission("agent-1", perm1)
        permission_manager.grant_permission("agent-1", perm2)

        perms = permission_manager.get_agent_permissions("agent-1")
        assert len(perms) == 2

    def test_multiple_agents(self) -> None:
        """Test managing permissions for multiple agents."""
        perm1 = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read-only",
        )
        perm2 = ToolPermission(
            category=PermissionCategory.EXECUTE_COMMAND,
            level=PermissionLevel.ALLOW,
            description="Execute",
        )
        permission_manager.grant_permission("agent-1", perm1)
        permission_manager.grant_permission("agent-2", perm2)

        assert permission_manager.has_permission("agent-1", PermissionCategory.READ_ONLY)
        assert not permission_manager.has_permission("agent-1", PermissionCategory.EXECUTE_COMMAND)
        assert permission_manager.has_permission("agent-2", PermissionCategory.EXECUTE_COMMAND)


class TestApprovalManager:
    """Tests for ApprovalManager."""

    def setup_method(self) -> None:
        permission_manager.clear()
        approval_manager.clear()
        audit_log.clear()

    def test_request_and_grant_approval_records_audit(self) -> None:
        """Test explicit approval grant is tracked and auditable."""
        task_id = TaskId()
        agent_id = AgentId(id="agent-1")
        request = approval_manager.request_approval(
            task_id=task_id,
            agent_id=agent_id,
            action_type="tool:repository_write_file:local_write",
            reason="Write a file",
            risk_level="high",
            user_id="user-1",
        )

        granted = approval_manager.decide(request.request_id, approved=True, user_id="reviewer-1")

        assert granted.approved is True
        events = audit_log.get_events(task_id=task_id)
        assert [event.event_type for event in events] == [
            AuditEventType.APPROVAL_REQUESTED,
            AuditEventType.APPROVAL_GRANTED,
        ]

    def test_request_and_deny_approval_records_audit(self) -> None:
        """Test explicit approval denial is tracked and auditable."""
        task_id = TaskId()
        agent_id = AgentId(id="agent-1")
        request = approval_manager.request_approval(
            task_id=task_id,
            agent_id=agent_id,
            action_type="tool:repository_write_file:local_write",
            reason="Write a file",
            risk_level="high",
        )

        denied = approval_manager.decide(request.request_id, approved=False)

        assert denied.approved is False
        events = audit_log.get_events(task_id=task_id)
        assert [event.event_type for event in events] == [
            AuditEventType.APPROVAL_REQUESTED,
            AuditEventType.APPROVAL_DENIED,
        ]
