"""Authentication and engagement-scoped authorization for BABY API."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Set

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from baby.configuration import settings

security_bearer = HTTPBearer(auto_error=False)


class Role(str, Enum):
    """Caller roles for access control."""

    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


@dataclass
class AuthenticatedCaller:
    """Security principal representing an authenticated API caller."""

    caller_id: str
    role: Role
    allowed_engagements: Set[str] = field(default_factory=set)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN or "*" in self.allowed_engagements

    def can_access_engagement(self, engagement_id: Optional[str]) -> bool:
        """Verify caller is authorized to view records for this engagement/user."""
        if self.is_admin:
            return True
        if engagement_id is None:
            # Non-admin callers cannot access unscoped/global data when scoped
            return False
        return engagement_id in self.allowed_engagements


def get_current_caller(
    auth_header: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    x_engagement_id: Optional[str] = Header(None, alias="X-Engagement-ID"),
) -> AuthenticatedCaller:
    """Validate credentials and construct caller principal.

    Fail-closed: Rejects missing or invalid tokens when api_require_auth is True.
    """
    token = None
    if auth_header and auth_header.credentials:
        token = auth_header.credentials.strip()
    elif x_api_key:
        token = x_api_key.strip()

    if settings.api_require_auth:
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Check admin token
        if settings.api_admin_token and token == settings.api_admin_token:
            return AuthenticatedCaller(
                caller_id="admin-user",
                role=Role.ADMIN,
                allowed_engagements={"*"},
            )

        # Check standard operator/api token
        if settings.api_auth_token and token == settings.api_auth_token:
            # Scoped to X-Engagement-ID header if provided, or default operator scope
            engagements = {x_engagement_id} if x_engagement_id else {"default"}
            return AuthenticatedCaller(
                caller_id="operator-user",
                role=Role.OPERATOR,
                allowed_engagements=engagements,
            )

        # If neither configured token matches
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Auth not strictly enforced (e.g. local test mode without configured tokens)
    engagements = {x_engagement_id} if x_engagement_id else {"*"}
    return AuthenticatedCaller(
        caller_id="local-viewer",
        role=Role.ADMIN if "*" in engagements else Role.VIEWER,
        allowed_engagements=engagements,
    )
