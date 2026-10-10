/**
 * Main entry point for the typed BABY API Client.
 *
 * Example usage:
 * ```ts
 * import { BabyClient } from "@baby/client";
 *
 * const client = new BabyClient({
 *   baseUrl: "http://localhost:8000",
 *   token: "my-auth-token",
 *   engagementId: "eng-1",
 * });
 *
 * const task = await client.tasks.get("3b07229c-1c5a-4820-a214-eb8b8667f5ca");
 * console.log(task.title, task.status);
 * ```
 */

import { ObservabilityApi, TasksApi, WorkersApi } from "./apis.js";
import { HttpTransport, type TransportConfig } from "./transport.js";

export interface BabyClientConfig extends TransportConfig {
  /** API version prefix. Defaults to "/api/v1". */
  apiPrefix?: string;
}

export class BabyClient {
  readonly transport: HttpTransport;
  readonly tasks: TasksApi;
  readonly workers: WorkersApi;
  readonly observability: ObservabilityApi;

  constructor(config: BabyClientConfig) {
    this.transport = new HttpTransport(config);
    const prefix = config.apiPrefix ?? "/api/v1";

    this.tasks = new TasksApi(this.transport, prefix);
    this.workers = new WorkersApi(this.transport, prefix);
    this.observability = new ObservabilityApi(this.transport, prefix);
  }
}
