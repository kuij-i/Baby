/**
 * Typed client modules for BABY subsystems:
 * - TasksApi: submit, get, list, cancel, approvals
 * - WorkersApi: get, list
 * - ObservabilityApi: health, readiness, metrics, audit, agents
 */

import { HttpTransport, type RequestOptions } from "./transport.js";
import type {
  AgentView,
  ApprovalDecision,
  ApprovalDecisionResponse,
  AuditEventType,
  AuditEventView,
  ReadinessResponse,
  SystemHealth,
  TaskCancelResponse,
  TaskStatus,
  TaskSubmission,
  TaskSubmitResponse,
  TaskView,
  WorkerStatus,
  WorkerView,
} from "./types.js";

export interface ListTasksOptions {
  status?: TaskStatus;
  engagementId?: string;
  limit?: number;
  offset?: number;
}

export interface SubmitTaskOptions {
  /**
   * Optional `Idempotency-Key` header (1-255 visible ASCII characters, e.g. a UUID).
   * Reusing the key with an identical submission returns the original task
   * (`replayed: true`) instead of creating a duplicate; a different submission
   * with the same key fails with `ConflictError` (409). Sent only as a header,
   * never in the URL or body.
   */
  idempotencyKey?: string;
}

export interface ListWorkersOptions {
  status?: WorkerStatus;
}

export interface ListAuditOptions {
  taskId?: string;
  agentId?: string;
  eventType?: AuditEventType;
  engagementId?: string;
  userId?: string;
  startTime?: string;
  endTime?: string;
  limit?: number;
  offset?: number;
}

export class TasksApi {
  constructor(private readonly transport: HttpTransport, private readonly prefix: string = "/api/v1") {}

  /** List operational tasks scoped to caller's engagement. */
  async list(options: ListTasksOptions = {}): Promise<TaskView[]> {
    const query: Record<string, string | number | boolean | null | undefined> = {};
    if (options.status) query["status"] = options.status;
    if (options.engagementId) query["engagement_id"] = options.engagementId;
    if (options.limit !== undefined) query["limit"] = options.limit;
    if (options.offset !== undefined) query["offset"] = options.offset;

    return this.transport.request<TaskView[]>(`${this.prefix}/tasks`, { query });
  }

  /** Retrieve operational record for a specific task by UUID. */
  async get(taskId: string): Promise<TaskView> {
    return this.transport.request<TaskView>(`${this.prefix}/tasks/${taskId}`);
  }

  /**
   * Durably enqueue a task for asynchronous execution by worker processes.
   *
   * Pass `options.idempotencyKey` to make retries of the same submission safe.
   * The client itself never retries a submission.
   */
  async submit(submission: TaskSubmission, options: SubmitTaskOptions = {}): Promise<TaskSubmitResponse> {
    const request: RequestOptions = { method: "POST", body: submission };
    if (options.idempotencyKey !== undefined) {
      request.headers = { "Idempotency-Key": options.idempotencyKey };
    }
    return this.transport.request<TaskSubmitResponse>(`${this.prefix}/tasks/submit`, request);
  }

  /** Cancel an in-progress or pending task. */
  async cancel(taskId: string): Promise<TaskCancelResponse> {
    return this.transport.request<TaskCancelResponse>(`${this.prefix}/tasks/${taskId}/cancel`, {
      method: "POST",
    });
  }

  /** Approve or deny a pending high-risk approval request. Requires admin role. */
  async decideApproval(taskId: string, requestId: string, decision: ApprovalDecision): Promise<ApprovalDecisionResponse> {
    return this.transport.request<ApprovalDecisionResponse>(
      `${this.prefix}/tasks/${taskId}/approvals/${requestId}`,
      {
        method: "POST",
        body: decision,
      }
    );
  }
}

export class WorkersApi {
  constructor(private readonly transport: HttpTransport, private readonly prefix: string = "/api/v1") {}

  /** List registered workers and their operational status. */
  async list(options: ListWorkersOptions = {}): Promise<WorkerView[]> {
    const query: Record<string, string | number | boolean | null | undefined> = {};
    if (options.status) query["status"] = options.status;

    return this.transport.request<WorkerView[]>(`${this.prefix}/workers`, { query });
  }

  /** Retrieve operational status for a specific worker. */
  async get(workerId: string): Promise<WorkerView> {
    return this.transport.request<WorkerView>(`${this.prefix}/workers/${workerId}`);
  }
}

export class ObservabilityApi {
  constructor(private readonly transport: HttpTransport, private readonly prefix: string = "/api/v1") {}

  /** Check liveness of the process. */
  async getHealth(): Promise<SystemHealth> {
    return this.transport.request<SystemHealth>(`${this.prefix}/health`);
  }

  /** Check readiness to safely accept work. */
  async getReadiness(): Promise<ReadinessResponse> {
    return this.transport.request<ReadinessResponse>(`${this.prefix}/readiness`);
  }

  /** Get snapshot of bounded operational metrics. */
  async getMetrics(): Promise<Record<string, unknown>> {
    return this.transport.request<Record<string, unknown>>(`${this.prefix}/metrics`);
  }

  /** List filtered, engagement-scoped audit records. */
  async listAuditEvents(options: ListAuditOptions = {}): Promise<AuditEventView[]> {
    const query: Record<string, string | number | boolean | null | undefined> = {};
    if (options.taskId) query["task_id"] = options.taskId;
    if (options.agentId) query["agent_id"] = options.agentId;
    if (options.eventType) query["event_type"] = options.eventType;
    if (options.engagementId) query["engagement_id"] = options.engagementId;
    if (options.userId) query["user_id"] = options.userId;
    if (options.startTime) query["start_time"] = options.startTime;
    if (options.endTime) query["end_time"] = options.endTime;
    if (options.limit !== undefined) query["limit"] = options.limit;
    if (options.offset !== undefined) query["offset"] = options.offset;

    return this.transport.request<AuditEventView[]>(`${this.prefix}/audit`, { query });
  }

  /** List registered agents. */
  async listAgents(): Promise<AgentView[]> {
    return this.transport.request<AgentView[]>(`${this.prefix}/agents`);
  }

  /** Retrieve details for a specific registered agent. */
  async getAgent(agentId: string): Promise<AgentView> {
    return this.transport.request<AgentView>(`${this.prefix}/agents/${agentId}`);
  }
}
