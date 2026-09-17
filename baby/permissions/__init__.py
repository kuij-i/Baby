"""Permission management for BABY.

Provides authorization checks and permission enforcement.
"""

from baby.permissions.approval import ApprovalManager, approval_manager
from baby.permissions.manager import PermissionManager, permission_manager

__all__ = [
    "approval_manager",
    "ApprovalManager",
    "permission_manager",
    "PermissionManager",
]
