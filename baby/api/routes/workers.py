"""Read-only worker visibility endpoints."""

from typing import Any, Dict, List, Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status

from baby.api.auth import AuthenticatedCaller, get_current_caller
from baby.core import WorkerStatus
from baby.observability.redaction import redact_sensitive_data
from baby.observability.workers import worker_tracker

router = APIRouter(tags=["Workers"])


@router.get("/workers", response_model=List[dict])
def list_workers(
    status_filter: Optional[WorkerStatus] = Query(None, alias="status"),
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> List[dict]:
    """Expose safe operational visibility into background/pool workers."""
    workers = worker_tracker.list_workers(status=status_filter)
    return cast(List[Dict[str, Any]], [redact_sensitive_data(w.model_dump()) for w in workers])


@router.get("/workers/{worker_id}", response_model=dict)
def get_worker(
    worker_id: str,
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> dict:
    """Retrieve operational state for a specific worker."""
    worker = worker_tracker.get_worker(worker_id)
    if not worker:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worker '{worker_id}' not found",
        )
    return cast(Dict[str, Any], redact_sensitive_data(worker.model_dump()))
