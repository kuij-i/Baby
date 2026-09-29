"""Readiness check endpoint for BABY.

Readiness is distinct from liveness (/health).
- Liveness (/health): Is the process alive and responsive?
- Readiness (/ready, /readiness): Is Baby initialized and ready to safely accept work?

Readiness verifies:
1. Persistence / memory store availability
2. Configuration validity (via validate_settings)
3. Worker subsystem operational state
4. Audit subsystem availability

Both endpoints are strictly read-only and execute no tasks, tools, or side effects.
"""

from typing import Any, Dict, List

from fastapi import APIRouter, Response, status

from baby.configuration import settings, validate_settings
from baby.observability.health import HealthStatus, health_checker
from baby.observability.workers import worker_tracker

router = APIRouter(tags=["Readiness"])


def _check_system_readiness() -> tuple[bool, List[str], Dict[str, Any]]:
    """Inspect system readiness without side effects.

    Returns:
        (is_ready, issues_list, details_dict)
    """
    issues: List[str] = []
    details: Dict[str, Any] = {}

    # 1. Persistence / memory subsystem check
    mem_health = health_checker.check_memory()
    details["persistence"] = mem_health.status.value
    if mem_health.status == HealthStatus.UNAVAILABLE:
        issues.append("Persistence/memory subsystem is unavailable")

    # 2. Audit subsystem check
    audit_health = health_checker.check_audit()
    details["audit"] = audit_health.status.value
    if audit_health.status == HealthStatus.UNAVAILABLE:
        issues.append("Audit subsystem is unavailable")

    # 3. Worker subsystem operational check
    try:
        workers = worker_tracker.list_workers()
        details["workers"] = {
            "total": len(workers),
            "status": "operational",
        }
    except Exception:
        issues.append("Worker subsystem check failed")
        details["workers"] = {"status": "unavailable"}

    # 4. Configuration validation
    try:
        cfg_warnings = validate_settings(settings)
        details["configuration"] = {
            "valid": True,
            "warnings_count": len(cfg_warnings),
        }
    except Exception:
        issues.append("Configuration validation failed")
        details["configuration"] = {"valid": False}

    # 5. Auth configuration when auth is required
    if settings.api_require_auth:
        has_token = bool(settings.api_auth_token and settings.api_auth_token.strip())
        has_admin = bool(settings.api_admin_token and settings.api_admin_token.strip())
        if not has_token and not has_admin:
            issues.append("api_require_auth=True but no API tokens configured")

    is_ready = len(issues) == 0
    return is_ready, issues, details


@router.get("/ready")
@router.get("/readiness")
def get_readiness(response: Response) -> Dict[str, Any]:
    """Readiness check: confirms Baby can safely accept and process work.

    Returns 200 OK when ready, 503 Service Unavailable when not ready.
    Available under both /ready and /readiness.
    """
    is_ready, issues, details = _check_system_readiness()

    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "ready": False,
            "status": "not_ready",
            "issues": issues,
            "details": details,
        }

    response.status_code = status.HTTP_200_OK
    return {
        "ready": True,
        "status": "ready",
        "issues": [],
        "details": details,
    }
