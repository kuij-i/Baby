"""Domain contracts for the BABY orchestration platform.

These contracts define the core types and interfaces that form the foundation
of the BABY system. All major components communicate using these types.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


class TaskId(BaseModel):
    """Unique identifier for a task."""

    id: UUID = Field(default_factory=uuid4)

    def __str__(self) -> str:
        return str(self.id)

    def __hash__(self) -> int:
        return hash(self.id)


class AgentId(BaseModel):
    """Unique identifier for an agent."""

    id: str = Field(..., min_length=1, max_length=256)

    def __str__(self) -> str:
        return self.id

    def __hash__(self) -> int:
        return hash(self.id)


class PermissionLevel(str, Enum):
    """Permission decision levels."""

    ALLOW = "allow"
    DENY = "deny"
    APPROVAL_REQUIRED = "approval_required"


class PermissionCategory(str, Enum):
    """Permission categories."""

    READ_ONLY = "read_only"
    LOCAL_READ = "local_read"
    LOCAL_WRITE = "local_write"
    EXECUTE_COMMAND = "execute_command"
    NETWORK_ACCESS = "network_access"
    EXTERNAL_API = "external_api"
    GITHUB_READ = "github_read"
    GITHUB_WRITE = "github_write"
    FINANCIAL_ACTION = "financial_action"
    SECURITY_ACTION = "security_action"
    DESTRUCTIVE_ACTION = "destructive_action"
    SECRET_ACCESS = "secret_access"


class ToolPermission(BaseModel):
    """Permission specification for a tool."""

    category: PermissionCategory
    level: PermissionLevel = PermissionLevel.ALLOW
    description: str = Field(..., min_length=1)
    constraints: Optional[Dict[str, Any]] = None


class AgentCapability(BaseModel):
    """Capability of an agent."""

    name: str = Field(..., min_length=1, max_length=256)
    description: str = Field(..., min_length=1)
    required_permissions: List[PermissionCategory] = Field(default_factory=list)


class ToolSpec(BaseModel):
    """Specification for a tool that agents can use."""

    name: str = Field(..., min_length=1, max_length=256)
    description: str = Field(..., min_length=1)
    version: str = Field(default="1.0.0")
    permissions_required: List[ToolPermission]
    input_schema: Dict[str, Any]
    output_schema: Optional[Dict[str, Any]] = None
    timeout_seconds: int = Field(default=300, gt=0)
    retry_count: int = Field(default=0, ge=0)


class AgentSpec(BaseModel):
    """Specification for an agent."""

    id: AgentId
    name: str = Field(..., min_length=1, max_length=256)
    description: str = Field(..., min_length=1)
    role: str = Field(..., min_length=1)
    capabilities: List[AgentCapability] = Field(default_factory=list)
    permissions: List[ToolPermission] = Field(default_factory=list)
    max_budget: Optional[int] = None  # Token budget
    context_window: int = Field(default=8000, gt=0)
    model: str = Field(default="gpt-4")  # Model provider/identifier
    enabled: bool = True


class Task(BaseModel):
    """A task to be executed by the orchestrator."""

    id: TaskId = Field(default_factory=TaskId)
    title: str = Field(..., min_length=1)
    description: str
    created_at: datetime = Field(default_factory=utc_now)
    user_id: Optional[str] = None
    priority: int = Field(default=0, ge=0, le=10)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class PlanStep(BaseModel):
    """A step in a plan."""

    step_id: int = Field(..., ge=1)
    description: str = Field(..., min_length=1)
    agent_id: Optional[AgentId] = None
    tool_name: Optional[str] = None
    dependencies: List[int] = Field(default_factory=list)  # Step IDs this depends on
    approval_required: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    """A plan for executing a task."""

    task_id: TaskId
    steps: List[PlanStep] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    reasoning: str = Field(..., min_length=1)


class ExecutionContext(BaseModel):
    """Context for executing a plan step."""

    task_id: TaskId
    plan_step: PlanStep
    task: Task
    agent: AgentSpec
    previous_results: Dict[int, "AgentResult"] = Field(default_factory=dict)


class AgentResult(BaseModel):
    """Result of agent execution."""

    step_id: int
    agent_id: AgentId
    success: bool
    output: Any = None
    error: Optional[str] = None
    execution_time_ms: int = Field(default=0, ge=0)
    tokens_used: Optional[int] = None
    timestamp: datetime = Field(default_factory=utc_now)


class VerificationResult(BaseModel):
    """Result of verifying agent output."""

    agent_result_id: int
    agent_id: AgentId
    verified: bool
    verification_method: str
    issues: List[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=utc_now)


class ApprovalRequest(BaseModel):
    """Request for approval of a high-risk action."""

    request_id: UUID = Field(default_factory=uuid4)
    task_id: TaskId
    agent_id: AgentId
    action_type: str = Field(..., min_length=1)
    reason: str = Field(..., min_length=1)
    risk_level: str = Field(...)  # low, medium, high, critical
    created_at: datetime = Field(default_factory=utc_now)
    requires_approval: bool = True
    approved: Optional[bool] = None
    approval_timestamp: Optional[datetime] = None


class Artifact(BaseModel):
    """An artifact created by an agent."""

    id: UUID = Field(default_factory=uuid4)
    task_id: TaskId
    agent_id: AgentId
    name: str = Field(..., min_length=1)
    artifact_type: str = Field(..., min_length=1)
    content: Any
    created_at: datetime = Field(default_factory=utc_now)
    verified: bool = False


class MemoryRecord(BaseModel):
    """A record stored in memory."""

    id: UUID = Field(default_factory=uuid4)
    task_id: Optional[TaskId] = None
    agent_id: Optional[AgentId] = None
    record_type: str = Field(..., min_length=1)  # preference, decision, fact, history
    content: Dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: Optional[datetime] = None


class AuditEventType(str, Enum):
    """Types of audit events."""

    TASK_CREATED = "task_created"
    PLAN_CREATED = "plan_created"
    AGENT_SELECTED = "agent_selected"
    PERMISSION_CHECKED = "permission_checked"
    TOOL_INVOKED = "tool_invoked"
    TOOL_RESULT = "tool_result"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    ARTIFACT_CREATED = "artifact_created"
    VERIFICATION_COMPLETED = "verification_completed"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    ERROR = "error"


class AuditEvent(BaseModel):
    """An audit event."""

    id: UUID = Field(default_factory=uuid4)
    event_type: AuditEventType
    task_id: Optional[TaskId] = None
    agent_id: Optional[AgentId] = None
    timestamp: datetime = Field(default_factory=utc_now)
    details: Dict[str, Any] = Field(default_factory=dict)
    user_id: Optional[str] = None

    model_config = ConfigDict(use_enum_values=True)
