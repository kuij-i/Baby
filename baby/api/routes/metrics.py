"""Read-only operational metrics endpoint."""

from typing import Any, Dict

from fastapi import APIRouter, Depends

from baby.api.auth import AuthenticatedCaller, get_current_caller
from baby.observability.metrics import metrics_collector

router = APIRouter(tags=["Metrics"])


@router.get("/metrics", response_model=Dict[str, Any])
def get_metrics(
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> Dict[str, Any]:
    """Expose snapshot of bounded operational metrics."""
    return metrics_collector.get_snapshot()
