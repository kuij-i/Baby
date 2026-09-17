"""Tests for permission system."""

import pytest

from baby.core import (
    PermissionCategory,
    PermissionLevel,
    ToolPermission,
)
from baby.errors import PermissionDeniedError
from baby.permissions.manager import permission_manager


class TestPermissionManager:
    """Tests for PermissionManager."""

    def setup_method(self) -> None:
        """Clear permissions before each test."""
        permission_manager.clear()

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

    def test_check_permission_explicit_deny(self) -> None:
        """Test explicitly denied permissions stay denied."""
        permission_manager.grant_permission(
            "agent-1",
            ToolPermission(
                category=PermissionCategory.DESTRUCTIVE_ACTION,
                level=PermissionLevel.DENY,
                description="Denied destructive action",
            ),
        )

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
