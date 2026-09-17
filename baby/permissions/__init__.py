"""Permission management for BABY.

Provides authorization checks and permission enforcement.
"""

from baby.permissions.manager import PermissionManager, permission_manager

__all__ = ["permission_manager", "PermissionManager"]
