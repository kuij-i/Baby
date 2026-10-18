"""Read-only operational metrics endpoint."""

from typing import Any, Dict

from fastapi import APIRouter, Depends

from baby.api.auth import AuthenticatedCaller, get_current_caller
from baby.core import TaskStatus
from baby.logging import get_logger
from baby.observability.metrics import metrics_collector
from baby.observability.tasks import task_tracker

router = APIRouter(tags=["Metrics"])
logger = get_logger(__name__)


def _current_task_counts(caller: AuthenticatedCaller) -> Dict[str, float]:
    """Tasks per status right now, from the durable store, limited to the caller's engagements."""
    counts: Dict[str, float] = {}
    for task_status in TaskStatus:
        if caller.is_admin:
            total = task_tracker.count_tasks(status=task_status)
        else:
            total = sum(
                task_tracker.count_tasks(user_id=engagement, status=task_status)
                for engagement in sorted(caller.allowed_engagements)
            )
        counts[f"status={task_status.value}"] = float(total)
    return counts


@router.get("/metrics", response_model=Dict[str, Any])
def get_metrics(
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> Dict[str, Any]:
    """Expose snapshot of bounded operational metrics.

    Counters and latencies describe events seen by this API process. Tasks run in worker
    processes, so ``gauges.tasks_by_status`` is read from the durable store instead of
    in-process counters (which would show every task as still pending).
    """
    snapshot = metrics_collector.get_snapshot()
    if task_tracker.store is not None:
        try:
            snapshot["gauges"]["tasks_by_status"] = _current_task_counts(caller)
            snapshot["counters"].pop("tasks_by_status", None)
        except Exception as exc:
            logger.warning("Could not read task counts from the durable store", error=str(exc))
    return snapshot
