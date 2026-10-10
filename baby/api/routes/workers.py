"""Read-only worker visibility endpoints.

Shows both in-process tracked workers (``worker_tracker``) and durable worker processes
registered in SQLite. Worker IDs are global, so for non-admin callers durable worker
records are reduced to operational status: host/process metadata is withheld and the
current task ID is shown only when the caller may access that task's engagement.
"""

from typing import Any, Dict, List, Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Query, status

from baby.api.auth import AuthenticatedCaller, get_current_caller
from baby.api.schemas import WorkerView
from baby.core import WorkerInfo, WorkerStatus
from baby.logging import get_logger
from baby.observability.redaction import redact_sensitive_data
from baby.observability.tasks import task_tracker
from baby.observability.workers import worker_tracker

router = APIRouter(tags=["Workers"])
logger = get_logger(__name__)


def _durable_workers(status_filter: Optional[WorkerStatus] = None) -> List[WorkerInfo]:
    store = task_tracker.store
    if store is None:
        return []
    from baby.persistence.sqlite_workers import SQLiteWorkerStore

    try:
        return SQLiteWorkerStore(store.db).list(status=status_filter)
    except Exception as exc:
        logger.warning("Could not read durable worker registry", error=str(exc))
        return []


def _durable_view(worker: WorkerInfo, caller: AuthenticatedCaller) -> Dict[str, Any]:
    data: Dict[str, Any] = worker.model_dump()
    data["durable"] = True
    if caller.is_admin:
        return data
    data["metadata"] = {}
    data["last_error"] = None
    if worker.current_task_id is not None:
        record = task_tracker.get_task(worker.current_task_id)
        if record is None or not caller.can_access_engagement(record.user_id):
            data["current_task_id"] = None
    return data


@router.get("/workers", response_model=List[WorkerView])
def list_workers(
    status_filter: Optional[WorkerStatus] = Query(None, alias="status"),
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> List[dict]:
    """Expose safe operational visibility into background/pool workers."""
    workers = [w.model_dump() for w in worker_tracker.list_workers(status=status_filter)]
    known = {w["worker_id"] for w in workers}
    workers.extend(_durable_view(w, caller) for w in _durable_workers(status_filter) if w.worker_id not in known)
    return cast(List[Dict[str, Any]], [redact_sensitive_data(w) for w in workers])


@router.get("/workers/{worker_id}", response_model=WorkerView)
def get_worker(
    worker_id: str,
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> dict:
    """Retrieve operational state for a specific worker."""
    worker = worker_tracker.get_worker(worker_id)
    if worker:
        return cast(Dict[str, Any], redact_sensitive_data(worker.model_dump()))
    store = task_tracker.store
    if store is not None:
        from baby.persistence.sqlite_workers import SQLiteWorkerStore

        durable = SQLiteWorkerStore(store.db).get(worker_id)
        if durable is not None:
            return cast(Dict[str, Any], redact_sensitive_data(_durable_view(durable, caller)))
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Worker '{worker_id}' not found",
    )
