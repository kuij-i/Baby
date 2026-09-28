"""Authentication and engagement-scoped authorization for BABY API.

Security model:
- Authentication answers WHO IS THE CALLER (via token matching).
- Authorization answers WHAT IS THIS CALLER ALLOWED TO ACCESS (via server-side config).

CRITICAL: The X-Engagement-ID header identifies the REQUESTED RESOURCE scope.
It does NOT grant authorization to that scope.

Operator tokens have a fixed set of allowed_engagements configured server-side
via BABY_OPERATOR_ENGAGEMENTS (comma-separated). The header is used to SELECT
which of the caller's pre-authorized engagements to scope the query to, not to
expand the caller's authorization.

When api_require_auth=False the system runs in development-only permissive mode.
This must NEVER be used in production.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Set

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from baby.configuration import settings
from baby.logging import get_logger

logger = get_logger(__name__)

security_bearer = HTTPBearer(auto_error=False)


class Role(str, Enum):
    """Caller roles for access control."""

    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"


@dataclass
class AuthenticatedCaller:
    """Security principal representing an authenticated API caller.

    allowed_engagements is set SERVER-SIDE at authentication time based on
    trusted configuration. It cannot be expanded by request parameters.
    A wildcard value {"*"} indicates admin-level access to all engagements.
    """

    caller_id: str
    role: Role
    allowed_engagements: Set[str] = field(default_factory=set)

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN or "*" in self.allowed_engagements

    def can_access_engagement(self, engagement_id: Optional[str]) -> bool:
        """Verify caller is authorized to view records for this engagement/user.

        Authorization is checked against server-side allowed_engagements only.
        The caller cannot expand this set via request parameters.
        """
        if self.is_admin:
            return True
        if engagement_id is None:
            # Non-admin callers cannot access unscoped/global data
            return False
        return engagement_id in self.allowed_engagements

    def restrict_to_engagement(self, requested: Optional[str]) -> Optional[str]:
        """Return the effective engagement scope for a query.

        If the caller requests a specific engagement, verify they have access
        and return it. Otherwise default to the narrowest authorized scope.

        Raises HTTP 403 if the requested engagement is not in allowed_engagements.
        """
        if self.is_admin:
            # Admin may query any engagement (or all if unspecified)
            return requested

        if requested is not None:
            if not self.can_access_engagement(requested):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not authorized to access the requested engagement scope",
                )
            return requested

        # No specific engagement requested — default to caller's single allowed scope
        # (if multiple scopes, caller must explicitly specify one)
        if len(self.allowed_engagements) == 1:
            return next(iter(self.allowed_engagements))

        # Caller has zero or multiple allowed engagements but did not specify —
        # return None (caller to handle: either 403 or empty result)
        return None


def _parse_operator_engagements() -> Set[str]:
    """Read the configured set of engagements an operator token is authorized for.

    This is controlled entirely by the server via BABY_OPERATOR_ENGAGEMENTS env var.
    The client cannot influence this set.
    """
    raw = getattr(settings, "operator_engagements", None) or ""
    if not raw or not raw.strip():
        # Default: operator token is authorized for a single "default" engagement
        return {"default"}
    return {e.strip() for e in raw.split(",") if e.strip()}


def get_current_caller(
    auth_header: Optional[HTTPAuthorizationCredentials] = Depends(security_bearer),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    x_engagement_id: Optional[str] = Header(None, alias="X-Engagement-ID"),
) -> AuthenticatedCaller:
    """Validate credentials and construct caller principal.

    SECURITY CONTRACT:
    - allowed_engagements is set from server-side configuration ONLY.
    - X-Engagement-ID header identifies the requested resource scope,
      but NEVER grants additional authorization.
    - Fail-closed: Rejects missing or invalid tokens when api_require_auth=True.

    The x_engagement_id parameter is intentionally NOT used to set
    allowed_engagements. It is available to route handlers which call
    caller.restrict_to_engagement(x_engagement_id) to apply scoping
    WITHIN the caller's pre-authorized set.
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

        # Check admin token — server-configured, never derived from request headers
        if settings.api_admin_token and token == settings.api_admin_token:
            return AuthenticatedCaller(
                caller_id="admin-user",
                role=Role.ADMIN,
                allowed_engagements={"*"},
            )

        # Check standard operator/api token
        # allowed_engagements comes from server-side configuration only
        if settings.api_auth_token and token == settings.api_auth_token:
            operator_engagements = _parse_operator_engagements()
            return AuthenticatedCaller(
                caller_id="operator-user",
                role=Role.OPERATOR,
                allowed_engagements=operator_engagements,
            )

        # Neither configured token matches — reject
        logger.warning("API authentication failed: invalid token provided")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Auth not enforced — development/local mode ONLY.
    # IMPORTANT: This mode must NOT be used in production.
    # In this mode callers get full admin scope to allow local testing.
    # No engagement-scoping is applied from request headers.
    logger.debug("API running in unauthenticated development mode (api_require_auth=False)")
    return AuthenticatedCaller(
        caller_id="local-dev-user",
        role=Role.ADMIN,
        allowed_engagements={"*"},
    )
