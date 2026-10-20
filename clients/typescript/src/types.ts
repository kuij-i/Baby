/**
 * Auto-generated TypeScript API contracts for BABY AI Operating System.
 * Generated directly from OpenAPI 3.1 specification.
 * DO NOT EDIT MANUALLY. Regenerate with `npm run generate-types`.
 */

/** Schema for AgentCapabilityView */
export interface AgentCapabilityView {
  name: string;
  description: string;
  required_permissions?: PermissionCategory[];
}

/** Agent identifier wrapper (wire shape ``{"id": "<agent id>"}``). */
export interface AgentIdRef {
  id: string;
}

/** Schema for AgentResultView */
export interface AgentResultView {
  step_id: number;
  agent_id: AgentIdRef;
  success: boolean;
  output?: unknown;
  error?: string | null;
  execution_time_ms?: number;
  tokens_used?: number | null;
  timestamp: string;
}

/** Schema for AgentView */
export interface AgentView {
  id: string;
  name: string;
  description: string;
  role: string;
  enabled: boolean;
  model: string;
  context_window: number;
  max_budget?: number | null;
  capabilities?: AgentCapabilityView[];
  permissions?: ToolPermissionView[];
}

/** Request body for an approval decision. */
export interface ApprovalDecision {
  approved: boolean;
  reason?: string | null;
}

/** Schema for ApprovalDecisionResponse */
export interface ApprovalDecisionResponse {
  task_id: string;
  status: TaskStatus;
  approved: boolean;
}

/** An approval request bound to one task, engagement and exact operation. */
export interface ApprovalView {
  request_id: string;
  task_id: TaskIdRef;
  agent_id: AgentIdRef;
  action_type: string;
  reason: string;
  risk_level: string;
  created_at: string;
  requires_approval?: boolean;
  approved?: boolean | null;
  approval_timestamp?: string | null;
  engagement_id?: string | null;
  permission?: string | null;
  operation_digest?: string | null;
  operation_summary?: Record<string, unknown>;
  decided_by?: string | null;
  consumed_at?: string | null;
}

/** Types of audit events. */
export type AuditEventType = "task_created" | "plan_created" | "agent_selected" | "permission_checked" | "tool_invoked" | "tool_result" | "approval_requested" | "approval_granted" | "approval_denied" | "artifact_created" | "verification_completed" | "task_completed" | "task_failed" | "task_cancelled" | "task_claimed" | "task_lease_renewed" | "task_lease_lost" | "task_recovered" | "worker_started" | "worker_stopped" | "worker_heartbeat_failed" | "worker_stale" | "error";
export const AuditEventType = {
  TASK_CREATED: "task_created",
  PLAN_CREATED: "plan_created",
  AGENT_SELECTED: "agent_selected",
  PERMISSION_CHECKED: "permission_checked",
  TOOL_INVOKED: "tool_invoked",
  TOOL_RESULT: "tool_result",
  APPROVAL_REQUESTED: "approval_requested",
  APPROVAL_GRANTED: "approval_granted",
  APPROVAL_DENIED: "approval_denied",
  ARTIFACT_CREATED: "artifact_created",
  VERIFICATION_COMPLETED: "verification_completed",
  TASK_COMPLETED: "task_completed",
  TASK_FAILED: "task_failed",
  TASK_CANCELLED: "task_cancelled",
  TASK_CLAIMED: "task_claimed",
  TASK_LEASE_RENEWED: "task_lease_renewed",
  TASK_LEASE_LOST: "task_lease_lost",
  TASK_RECOVERED: "task_recovered",
  WORKER_STARTED: "worker_started",
  WORKER_STOPPED: "worker_stopped",
  WORKER_HEARTBEAT_FAILED: "worker_heartbeat_failed",
  WORKER_STALE: "worker_stale",
  ERROR: "error",
} as const;
export type AuditEventTypeValue = typeof AuditEventType[keyof typeof AuditEventType];

/** Schema for AuditEventView */
export interface AuditEventView {
  id: string;
  event_type: AuditEventType;
  task_id?: TaskIdRef | null;
  agent_id?: AgentIdRef | null;
  timestamp: string;
  details?: Record<string, unknown>;
  user_id?: string | null;
}

/** Health status for a single subsystem. */
export interface ComponentHealth {
  status: HealthStatus;
  message?: string;
  details?: Record<string, unknown>;
}

/** Schema for HTTPValidationError */
export interface HTTPValidationError {
  detail?: ValidationError[];
}

/** Health check status states. */
export type HealthStatus = "healthy" | "degraded" | "unavailable";
export const HealthStatus = {
  HEALTHY: "healthy",
  DEGRADED: "degraded",
  UNAVAILABLE: "unavailable",
} as const;
export type HealthStatusValue = typeof HealthStatus[keyof typeof HealthStatus];

/** Permission categories. */
export type PermissionCategory = "read_only" | "local_read" | "local_write" | "execute_command" | "network_access" | "external_api" | "github_read" | "github_write" | "financial_action" | "security_action" | "destructive_action" | "secret_access";
export const PermissionCategory = {
  READ_ONLY: "read_only",
  LOCAL_READ: "local_read",
  LOCAL_WRITE: "local_write",
  EXECUTE_COMMAND: "execute_command",
  NETWORK_ACCESS: "network_access",
  EXTERNAL_API: "external_api",
  GITHUB_READ: "github_read",
  GITHUB_WRITE: "github_write",
  FINANCIAL_ACTION: "financial_action",
  SECURITY_ACTION: "security_action",
  DESTRUCTIVE_ACTION: "destructive_action",
  SECRET_ACCESS: "secret_access",
} as const;
export type PermissionCategoryValue = typeof PermissionCategory[keyof typeof PermissionCategory];

/** Permission decision levels. */
export type PermissionLevel = "allow" | "deny" | "approval_required";
export const PermissionLevel = {
  ALLOW: "allow",
  DENY: "deny",
  APPROVAL_REQUIRED: "approval_required",
} as const;
export type PermissionLevelValue = typeof PermissionLevel[keyof typeof PermissionLevel];

/** Schema for PlanStepView */
export interface PlanStepView {
  step_id: number;
  description: string;
  agent_id?: AgentIdRef | null;
  tool_name?: string | null;
  dependencies?: number[];
  approval_required?: boolean;
}

/** Schema for PlanView */
export interface PlanView {
  task_id: TaskIdRef;
  steps: PlanStepView[];
  created_at: string;
  reasoning: string;
  approval_required?: boolean;
  planner_mode?: string;
}

/** Schema for ReadinessResponse */
export interface ReadinessResponse {
  ready: boolean;
  status: string;
  issues?: string[];
  details?: Record<string, unknown>;
}

/** Aggregated health status for the BABY platform. */
export interface SystemHealth {
  status: HealthStatus;
  timestamp?: string;
  version?: string;
  components?: Record<string, ComponentHealth>;
}

/** Schema for TaskCancelResponse */
export interface TaskCancelResponse {
  task_id: string;
  status: TaskStatus;
  cancel_requested: boolean;
}

/** Task identifier wrapper (wire shape ``{"id": "<uuid>"}``). */
export interface TaskIdRef {
  id: string;
}

/** Execution status of a task.

State machine:
    PENDING → IN_PROGRESS | CANCELLED
    IN_PROGRESS → AWAITING_APPROVAL | COMPLETED | FAILED | CANCELLED
    AWAITING_APPROVAL → IN_PROGRESS | FAILED | CANCELLED
    COMPLETED → (terminal — no further transitions)
    FAILED → (terminal — no further transitions)
    CANCELLED → (terminal — no further transitions) */
export type TaskStatus = "pending" | "in_progress" | "awaiting_approval" | "completed" | "failed" | "cancelled";
export const TaskStatus = {
  PENDING: "pending",
  IN_PROGRESS: "in_progress",
  AWAITING_APPROVAL: "awaiting_approval",
  COMPLETED: "completed",
  FAILED: "failed",
  CANCELLED: "cancelled",
} as const;
export type TaskStatusValue = typeof TaskStatus[keyof typeof TaskStatus];

/** Request body for durable task submission. */
export interface TaskSubmission {
  title: string;
  description: string;
  priority?: number;
  engagement_id?: string | null;
}

/** Schema for TaskSubmitResponse */
export interface TaskSubmitResponse {
  task_id: string;
  status: TaskStatus;
  engagement_id: string;
  replayed?: boolean;
}

/** Operational record for one task. ``status`` follows the authoritative state machine. */
export interface TaskView {
  task_id: TaskIdRef;
  title: string;
  description?: string;
  status: TaskStatus;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  user_id?: string | null;
  priority?: number;
  assigned_agent_id?: AgentIdRef | null;
  assigned_worker_id?: string | null;
  plan?: PlanView | null;
  result?: AgentResultView | null;
  error?: string | null;
  approvals?: ApprovalView[];
  verifications?: VerificationView[];
  cancel_requested?: boolean;
}

/** Schema for ToolPermissionView */
export interface ToolPermissionView {
  category: PermissionCategory;
  level: PermissionLevel;
  description: string;
  constraints?: Record<string, unknown> | null;
}

/** Schema for ValidationError */
export interface ValidationError {
  loc: (string | number)[];
  msg: string;
  type: string;
  input?: unknown;
  ctx?: Record<string, unknown>;
}

/** Schema for VerificationView */
export interface VerificationView {
  agent_result_id: number;
  verified: boolean;
  verification_method: string;
  issues?: string[];
  timestamp: string;
}

/** Operational status of a worker.

Durable worker processes move through:
    STARTING → IDLE ⇄ BUSY → STOPPING → STOPPED
and may be marked by recovery as:
    STALE  — heartbeat older than ``worker_stale_timeout_seconds``
    ERROR  — process exited abnormally or failed fatally
IDLE/BUSY/STARTING/STOPPING with a fresh heartbeat count as alive. */
export type WorkerStatus = "starting" | "idle" | "busy" | "paused" | "stopping" | "stopped" | "stale" | "error";
export const WorkerStatus = {
  STARTING: "starting",
  IDLE: "idle",
  BUSY: "busy",
  PAUSED: "paused",
  STOPPING: "stopping",
  STOPPED: "stopped",
  STALE: "stale",
  ERROR: "error",
} as const;
export type WorkerStatusValue = typeof WorkerStatus[keyof typeof WorkerStatus];

/** Worker operational status. Host/process metadata is withheld from non-admin callers. */
export interface WorkerView {
  worker_id: string;
  status: WorkerStatus;
  current_task_id?: TaskIdRef | null;
  tasks_completed?: number;
  tasks_failed?: number;
  started_at: string;
  last_heartbeat_at: string;
  last_error?: string | null;
  metadata?: Record<string, unknown>;
  stopped_at?: string | null;
  durable?: boolean;
}
