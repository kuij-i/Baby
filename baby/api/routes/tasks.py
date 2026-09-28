"""Read-only task visibility endpoints."""

from typing import Any, Dict, List, Optional, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from baby.api.auth import AuthenticatedCaller, get_current_caller
from baby.core import TaskId, TaskStatus
from baby.observability.redaction import redact_sensitive_data
from baby.observability.tasks import task_tracker

router = APIRouter(tags=["Tasks"])


@router.get("/tasks", response_model=List[dict])
def list_tasks(
    status_filter: Optional[TaskStatus] = Query(None, alias="status"),
    engagement_id: Optional[str] = Query(None, alias="engagement_id"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    x_engagement_id: Optional[str] = Header(None, alias="X-Engagement-ID"),
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> List[dict]:
    """Expose safe read-only operational information about tasks.

    Scope is determined by caller's server-side authorized engagements.
    The engagement_id query parameter selects within that set; it cannot
    expand the caller's authorization beyond what was configured server-side.
    """
    # Query param takes priority over header for explicit scoping;
    # restrict_to_engagement enforces it is within caller's authorized set.
    requested_scope = engagement_id or x_engagement_id
    effective_scope = caller.restrict_to_engagement(requested_scope)

    # If non-admin with no resolvable scope, return empty (safe default)
    if not caller.is_admin and effective_scope is None:
        return []

    records = task_tracker.list_tasks(
        user_id=effective_scope,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    # Redact sensitive data before returning
    return [cast(Dict[str, Any], redact_sensitive_data(r.model_dump())) for r in records]


@router.get("/tasks/{task_id}", response_model=dict)
def get_task(
    task_id: UUID,
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> dict:
    """Retrieve detailed read-only state for a specific task.

    IDOR protection: the task's stored user_id is checked against the caller's
    server-side authorized engagements. Knowing a task UUID is not sufficient
    to access it across engagement boundaries.
    """
    record = task_tracker.get_task(TaskId(id=task_id))
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    # Engagement isolation: non-admin callers cannot access other engagements' tasks
    if not caller.can_access_engagement(record.user_id):
        # Return 404 rather than 403 to avoid confirming existence of cross-scope tasks
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    return cast(Dict[str, Any], redact_sensitive_data(record.model_dump()))
