"""Permission manager for authorization checks."""

from typing import Dict, List, Optional

from baby.core import (
    PermissionCategory,
    PermissionLevel,
    ToolPermission,
)
from baby.errors import PermissionDeniedError


class PermissionManager:
    """Manages permissions and authorization checks.

    Determines if an agent can use a tool based on its permissions.
    """

    def __init__(self) -> None:
        """Initialize permission manager."""
        self._agent_permissions: Dict[str, List[ToolPermission]] = {}

    def grant_permission(self, agent_id: str, permission: ToolPermission) -> None:
        """Grant a permission to an agent.

        Args:
            agent_id: ID of agent
            permission: Permission to grant
        """
        if agent_id not in self._agent_permissions:
            self._agent_permissions[agent_id] = []

        self._agent_permissions[agent_id].append(permission)

    def revoke_permission(self, agent_id: str, category: PermissionCategory) -> None:
        """Revoke a permission category from an agent.

        Args:
            agent_id: ID of agent
            category: Permission category to revoke
        """
        if agent_id in self._agent_permissions:
            self._agent_permissions[agent_id] = [p for p in self._agent_permissions[agent_id] if p.category != category]

    def get_permission(self, agent_id: str, category: PermissionCategory) -> Optional[ToolPermission]:
        """Get a specific permission for an agent.

        Args:
            agent_id: ID of agent
            category: Permission category

        Returns:
            Permission or None if not granted
        """
        if agent_id not in self._agent_permissions:
            return None

        for perm in self._agent_permissions[agent_id]:
            if perm.category == category:
                return perm

        return None

    def has_permission(self, agent_id: str, category: PermissionCategory) -> bool:
        """Check if agent has permission for a category.

        Args:
            agent_id: ID of agent
            category: Permission category

        Returns:
            True if agent has permission
        """
        return self.get_permission(agent_id, category) is not None

    def check_permission(self, agent_id: str, required_permission: ToolPermission) -> PermissionLevel:
        """Check permission and return decision level.

        Args:
            agent_id: ID of agent
            required_permission: Required permission

        Returns:
            Permission level (ALLOW, DENY, or APPROVAL_REQUIRED)

        Raises:
            PermissionDeniedError: If permission is DENY
        """
        granted = self.get_permission(agent_id, required_permission.category)

        if granted is None:
            raise PermissionDeniedError(f"Agent {agent_id} lacks {required_permission.category.value} permission")

        if granted.level == PermissionLevel.DENY:
            raise PermissionDeniedError(f"Agent {agent_id} is denied {required_permission.category.value} permission")

        return granted.level

    def get_agent_permissions(self, agent_id: str) -> List[ToolPermission]:
        """Get all permissions for an agent.

        Args:
            agent_id: ID of agent

        Returns:
            List of granted permissions
        """
        return self._agent_permissions.get(agent_id, [])

    def clear(self) -> None:
        """Clear all permissions (for testing only)."""
        self._agent_permissions.clear()


# Global permission manager instance
permission_manager = PermissionManager()
