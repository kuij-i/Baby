"""Task visibility and task control endpoints.

Reads (GET) expose engagement-scoped task state. The only writes are task *control*
operations, which change durable task state and never execute anything in the API
process:

- ``POST /tasks/submit``                          enqueue a task as PENDING (operator/admin)
- ``POST /tasks/{task_id}/cancel``                 cancel through the state machine (operator/admin)
- ``POST /tasks/{task_id}/approvals/{request_id}`` approve/deny a pending approval (admin only)

Execution happens exclusively in worker processes (``python -m baby.workers run``).
"""

from typing import Any, Dict, List, Optional, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from baby.api.auth import AuthenticatedCaller, get_current_caller
from baby.api.schemas import (
    ApprovalDecisionResponse,
    TaskCancelResponse,
    TaskSubmitResponse,
    TaskView,
)
from baby.audit import audit_log
from baby.core import AuditEventType, Task, TaskId, TaskStatus
from baby.errors import ApprovalExpiredError, InvalidStateTransitionError, TaskNotFoundError
from baby.observability.redaction import redact_sensitive_data
from baby.observability.tasks import task_tracker

router = APIRouter(tags=["Tasks"])


@router.get("/tasks", response_model=List[TaskView])
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


@router.get("/tasks/{task_id}", response_model=TaskView)
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


class TaskSubmission(BaseModel):
    """Request body for durable task submission."""

    title: str = Field(..., min_length=1, max_length=256)
    description: str = Field(..., min_length=1, max_length=20_000)
    priority: int = Field(default=0, ge=0, le=10)
    engagement_id: Optional[str] = Field(default=None, min_length=1, max_length=256)

    model_config = {"extra": "forbid"}


class ApprovalDecision(BaseModel):
    """Request body for an approval decision."""

    approved: bool
    reason: Optional[str] = Field(default=None, max_length=1000)

    model_config = {"extra": "forbid"}


def _durable_queue():  # type: ignore[no-untyped-def]
    from baby.workers.queue import DurableTaskQueue

    store = task_tracker.store
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Durable task persistence is not configured",
        )
    return DurableTaskQueue(task_store=store)


def _get_scoped_task(task_id: UUID, caller: AuthenticatedCaller):  # type: ignore[no-untyped-def]
    record = task_tracker.get_task(TaskId(id=task_id))
    if not record or not caller.can_access_engagement(record.user_id):
        # 404 (not 403) so cross-engagement task IDs are not confirmed to exist
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task {task_id} not found")
    return record


@router.post("/tasks/submit", status_code=status.HTTP_202_ACCEPTED, response_model=TaskSubmitResponse)
def submit_task(
    body: TaskSubmission,
    x_engagement_id: Optional[str] = Header(None, alias="X-Engagement-ID"),
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> dict:
    """Durably enqueue a task for execution by a worker process.

    The task's engagement is resolved server-side from the caller's authorized scope;
    the request can only *select* one of the caller's engagements, never expand it.
    Nothing is executed by the API: a worker claims the task asynchronously.
    """
    effective = caller.restrict_to_engagement(body.engagement_id or x_engagement_id)
    if effective is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="An engagement_id within the caller's authorized scope is required",
        )
    task = Task(title=body.title, description=body.description, priority=body.priority, user_id=effective)
    record = _durable_queue().submit(task)
    audit_log.record(
        AuditEventType.TASK_CREATED,
        task_id=task.id,
        user_id=effective,
        details={"source": "api", "submitted_by": caller.caller_id, "priority": task.priority},
    )
    return {"task_id": str(record.task_id), "status": record.status.value, "engagement_id": effective}


@router.post("/tasks/{task_id}/cancel", response_model=TaskCancelResponse)
def cancel_task(
    task_id: UUID,
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> dict:
    """Cancel a task in the caller's engagement (immediately, or at the worker's next lease renewal)."""
    _get_scoped_task(task_id, caller)
    try:
        record = _durable_queue().request_cancel(TaskId(id=task_id))
    except TaskNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Task {task_id} not found")
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    audit_log.record(
        AuditEventType.TASK_CANCELLED,
        task_id=record.task_id,
        user_id=record.user_id,
        details={"requested_by": caller.caller_id, "status": record.status.value},
    )
    return {
        "task_id": str(record.task_id),
        "status": record.status.value,
        "cancel_requested": record.cancel_requested,
    }


@router.post("/tasks/{task_id}/approvals/{request_id}", response_model=ApprovalDecisionResponse)
def decide_approval(
    task_id: UUID,
    request_id: UUID,
    body: ApprovalDecision,
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> dict:
    """Approve or deny one pending approval request. Admin role required.

    The decision is bound to the task, its engagement and the exact operation digest
    recorded in the request; approving lets a worker execute that one operation once.
    A request older than ``approval_ttl_seconds`` can no longer be decided: the task is
    failed closed and 409 is returned.
    """
    from baby.permissions.approvals import approval_service

    if not caller.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required to decide approvals")
    scoped = _get_scoped_task(task_id, caller)
    try:
        record = approval_service.decide(
            TaskId(id=task_id), request_id, body.approved, decided_by=caller.caller_id, reason=body.reason
        )
    except TaskNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval request not found")
    except ApprovalExpiredError as exc:
        audit_log.record(
            AuditEventType.TASK_FAILED,
            task_id=TaskId(id=task_id),
            user_id=scoped.user_id,
            details={"request_id": str(request_id), "decided_by": caller.caller_id, "reason": "approval_expired"},
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except InvalidStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    audit_log.record(
        AuditEventType.APPROVAL_GRANTED if body.approved else AuditEventType.APPROVAL_DENIED,
        task_id=record.task_id,
        user_id=record.user_id,
        details={"request_id": str(request_id), "decided_by": caller.caller_id, "reason": body.reason},
    )
    return {"task_id": str(record.task_id), "status": record.status.value, "approved": body.approved}
