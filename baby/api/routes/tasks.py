"""Read-only task visibility endpoints."""

from typing import Any, Dict, List, Optional, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

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
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> List[dict]:
    """Expose safe read-only operational information about tasks."""
    # Scope enforcement
    if not caller.is_admin:
        if engagement_id and not caller.can_access_engagement(engagement_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to access the requested engagement scope",
            )
        if not engagement_id:
            # Default to the caller's allowed engagement
            if len(caller.allowed_engagements) == 1:
                engagement_id = next(iter(caller.allowed_engagements))
            elif not caller.allowed_engagements:
                return []

    records = task_tracker.list_tasks(
        user_id=engagement_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    # Redact sensitive data before returning
    return [redact_sensitive_data(r.model_dump()) for r in records]


@router.get("/tasks/{task_id}", response_model=dict)
def get_task(
    task_id: UUID,
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> dict:
    """Retrieve detailed read-only state for a specific task."""
    record = task_tracker.get_task(TaskId(id=task_id))
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    # Engagement isolation: non-admin callers cannot access other engagements' tasks
    if not caller.can_access_engagement(record.user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task {task_id} not found",
        )

    return cast(Dict[str, Any], redact_sensitive_data(record.model_dump()))
