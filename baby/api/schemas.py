"""Public API response schemas for the versioned ``/api/v1`` contract.

These models are the *wire* contract that the OpenAPI document (and therefore the generated
TypeScript types) is built from. They deliberately mirror the JSON the routes already
produce, so introducing them does not change any existing response, and they are separate
from the internal domain models in ``baby.core`` so that internals never leak into the
public contract by accident.

Compatibility note: identifiers are objects (``{"id": "<uuid>"}``) in task/audit/worker
payloads because that is the shape existing clients already receive. Changing that shape is
a breaking change and would require ``/api/v2``.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field

from baby.core import AuditEventType, PermissionCategory, PermissionLevel, TaskStatus, WorkerStatus


class TaskIdRef(BaseModel):
    """Task identifier wrapper (wire shape ``{"id": "<uuid>"}``)."""

    id: UUID


class AgentIdRef(BaseModel):
    """Agent identifier wrapper (wire shape ``{"id": "<agent id>"}``)."""

    id: str


class PlanStepView(BaseModel):
    step_id: int
    description: str
    agent_id: Optional[AgentIdRef] = None
    tool_name: Optional[str] = None
    dependencies: List[int] = Field(default_factory=list)
    approval_required: bool = False


class PlanView(BaseModel):
    task_id: TaskIdRef
    steps: List[PlanStepView]
    created_at: datetime
    reasoning: str


class AgentResultView(BaseModel):
    step_id: int
    agent_id: AgentIdRef
    success: bool
    output: Any = None
    error: Optional[str] = None
    execution_time_ms: int = 0
    tokens_used: Optional[Union[int, str]] = None
    timestamp: datetime


class VerificationView(BaseModel):
    agent_result_id: int
    verified: bool
    verification_method: str
    issues: List[str] = Field(default_factory=list)
    timestamp: datetime


class ApprovalView(BaseModel):
    """An approval request bound to one task, engagement and exact operation."""

    request_id: UUID
    task_id: TaskIdRef
    agent_id: AgentIdRef
    action_type: str
    reason: str
    risk_level: str
    created_at: datetime
    requires_approval: bool = True
    approved: Optional[bool] = None
    approval_timestamp: Optional[datetime] = None
    engagement_id: Optional[str] = None
    permission: Optional[str] = None
    operation_digest: Optional[str] = None
    operation_summary: Dict[str, Any] = Field(default_factory=dict)
    decided_by: Optional[str] = None
    consumed_at: Optional[datetime] = None


class TaskView(BaseModel):
    """Operational record for one task. ``status`` follows the authoritative state machine."""

    task_id: TaskIdRef
    title: str
    description: str = ""
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime] = None
    user_id: Optional[str] = None
    priority: int = 0
    assigned_agent_id: Optional[AgentIdRef] = None
    assigned_worker_id: Optional[str] = None
    plan: Optional[PlanView] = None
    result: Optional[AgentResultView] = None
    error: Optional[str] = None
    approvals: List[ApprovalView] = Field(default_factory=list)
    verifications: List[VerificationView] = Field(default_factory=list)
    cancel_requested: bool = False


class TaskSubmitResponse(BaseModel):
    task_id: UUID
    status: TaskStatus
    engagement_id: str


class TaskCancelResponse(BaseModel):
    task_id: UUID
    status: TaskStatus
    cancel_requested: bool


class ApprovalDecisionResponse(BaseModel):
    task_id: UUID
    status: TaskStatus
    approved: bool


class WorkerView(BaseModel):
    """Worker operational status. Host/process metadata is withheld from non-admin callers."""

    worker_id: str
    status: WorkerStatus
    current_task_id: Optional[TaskIdRef] = None
    tasks_completed: int = 0
    tasks_failed: int = 0
    started_at: datetime
    last_heartbeat_at: datetime
    last_error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    stopped_at: Optional[datetime] = None
    durable: bool = False


class AuditEventView(BaseModel):
    id: UUID
    event_type: AuditEventType
    task_id: Optional[TaskIdRef] = None
    agent_id: Optional[AgentIdRef] = None
    timestamp: datetime
    details: Dict[str, Any] = Field(default_factory=dict)
    user_id: Optional[str] = None


class ReadinessResponse(BaseModel):
    ready: bool
    status: str
    issues: List[str] = Field(default_factory=list)
    details: Dict[str, Any] = Field(default_factory=dict)


class AgentCapabilityView(BaseModel):
    name: str
    description: str
    required_permissions: List[PermissionCategory] = Field(default_factory=list)


class ToolPermissionView(BaseModel):
    category: PermissionCategory
    level: PermissionLevel
    description: str
    constraints: Optional[Dict[str, Any]] = None


class AgentView(BaseModel):
    id: str
    name: str
    description: str
    role: str
    enabled: bool
    model: str
    context_window: int
    max_budget: Optional[int] = None
    capabilities: List[AgentCapabilityView] = Field(default_factory=list)
    permissions: List[ToolPermissionView] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    """Stable error envelope returned for every non-validation error.

    ``code`` is the machine-readable discriminator clients should branch on; ``detail`` is a
    human-readable message that is never a raw exception string or stack trace.
    """

    detail: str
    code: str


class ValidationIssue(BaseModel):
    """One request validation problem (the offending input value is intentionally omitted)."""

    type: str
    loc: List[Any]
    msg: str


class ValidationErrorResponse(BaseModel):
    detail: List[ValidationIssue]
    code: str = "validation_error"
