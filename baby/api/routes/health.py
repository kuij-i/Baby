"""Read-only health check endpoint."""

from fastapi import APIRouter, Response, status

from baby.observability.health import HealthStatus, SystemHealth, health_checker

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=SystemHealth)
def get_health(response: Response) -> SystemHealth:
    """Read-only health representation distinguishing healthy, degraded, and unavailable states."""
    health = health_checker.check_health()
    if health.status == HealthStatus.UNAVAILABLE:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    else:
        response.status_code = status.HTTP_200_OK
    return health
