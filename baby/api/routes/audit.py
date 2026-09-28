"""Read-only audit visibility endpoints."""

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from baby.api.auth import AuthenticatedCaller, get_current_caller
from baby.audit import audit_log
from baby.core import AgentId, AuditEventType, TaskId
from baby.observability.redaction import redact_sensitive_data

router = APIRouter(tags=["Audit"])


@router.get("/audit", response_model=List[Dict[str, Any]])
def list_audit_events(
    task_id: Optional[UUID] = Query(None),
    agent_id: Optional[str] = Query(None),
    event_type: Optional[AuditEventType] = Query(None),
    engagement_id: Optional[str] = Query(None, alias="engagement_id"),
    user_id: Optional[str] = Query(None, alias="user_id"),
    start_time: Optional[datetime] = Query(None),
    end_time: Optional[datetime] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> List[Dict[str, Any]]:
    """Retrieve filtered, engagement-scoped audit records with sensitive details redacted."""
    target_user = user_id or engagement_id

    # Engagement isolation enforcement
    if not caller.is_admin:
        if target_user and not caller.can_access_engagement(target_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to access the requested engagement scope",
            )
        if not target_user:
            # Default to the caller's allowed engagement
            if len(caller.allowed_engagements) == 1:
                target_user = next(iter(caller.allowed_engagements))
            elif not caller.allowed_engagements:
                return []

    events = audit_log.get_events(
        task_id=TaskId(id=task_id) if task_id else None,
        agent_id=AgentId(id=agent_id) if agent_id else None,
        event_type=event_type,
        user_id=target_user,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
        offset=offset,
    )

    # Redact any credentials or secrets in event details
    result = []
    for event in events:
        event_dict = event.model_dump()
        result.append(redact_sensitive_data(event_dict))
    return result
