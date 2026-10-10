import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import {
  BabyClient,
  AuthenticationError,
  AuthorizationError,
  NotFoundError,
  ConflictError,
  ValidationError,
  ServerError,
  TimeoutError,
  NetworkError,
  TaskStatus,
  WorkerStatus,
  AuditEventType,
} from "../dist/index.js";

/**
 * Creates an ephemeral in-process HTTP server simulating the Baby API contracts.
 */
function createMockServer(handler: (req: http.IncomingMessage, res: http.ServerResponse) => void): Promise<{
  server: http.Server;
  url: string;
  close: () => Promise<void>;
}> {
  return new Promise((resolve, reject) => {
    const server = http.createServer(handler);
    server.listen(0, "127.0.0.1", () => {
      const addr = server.address();
      if (!addr || typeof addr === "string") {
        reject(new Error("Unable to obtain server address"));
        return;
      }
      const url = `http://127.0.0.1:${addr.port}`;
      resolve({
        server,
        url,
        close: () => new Promise<void>((res) => server.close(() => res())),
      });
    });
  });
}

test("TypeScript client E2E: Full task and observability round-trip", async () => {
  const taskId = "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d";
  const approvalId = "b2c3d4e5-f6a7-8b9c-0d1e-2f3a4b5c6d7e";
  const auditId = "c3d4e5f6-a7b8-9c0d-1e2f-3a4b5c6d7e8f";

  const { url, close } = await createMockServer((req, res) => {
    const reqUrl = new URL(req.url ?? "/", `http://${req.headers.host}`);
    const pathname = reqUrl.pathname;
    const method = req.method;

    // Check headers
    assert.equal(req.headers["authorization"], "Bearer test-operator-token");
    assert.equal(req.headers["x-engagement-id"], "eng-production");

    // Task submission
    if (pathname === "/api/v1/tasks/submit" && method === "POST") {
      let body = "";
      req.on("data", (chunk) => (body += chunk));
      req.on("end", () => {
        const parsed = JSON.parse(body);
        assert.equal(parsed.title, "Integration Test Task");
        res.writeHead(202, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            task_id: taskId,
            status: "pending",
            engagement_id: "eng-production",
          })
        );
      });
      return;
    }

    // Task retrieval
    if (pathname === `/api/v1/tasks/${taskId}` && method === "GET") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          task_id: { id: taskId },
          title: "Integration Test Task",
          description: "Testing worker pipeline",
          status: "in_progress",
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          user_id: "eng-production",
          priority: 5,
          assigned_worker_id: "worker-test-01",
          approvals: [],
          verifications: [],
          cancel_requested: false,
        })
      );
      return;
    }

    // Task cancellation
    if (pathname === `/api/v1/tasks/${taskId}/cancel` && method === "POST") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          task_id: taskId,
          status: "cancelled",
          cancel_requested: true,
        })
      );
      return;
    }

    // Approval decision
    if (pathname === `/api/v1/tasks/${taskId}/approvals/${approvalId}` && method === "POST") {
      let body = "";
      req.on("data", (chunk) => (body += chunk));
      req.on("end", () => {
        const parsed = JSON.parse(body);
        assert.equal(parsed.approved, true);
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            task_id: taskId,
            status: "in_progress",
            approved: true,
          })
        );
      });
      return;
    }

    // Workers list
    if (pathname === "/api/v1/workers" && method === "GET") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify([
          {
            worker_id: "worker-test-01",
            status: "busy",
            current_task_id: { id: taskId },
            tasks_completed: 4,
            tasks_failed: 0,
            started_at: new Date().toISOString(),
            last_heartbeat_at: new Date().toISOString(),
            durable: true,
            metadata: {},
          },
        ])
      );
      return;
    }

    // Health and Readiness
    if (pathname === "/api/v1/health" && method === "GET") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ status: "healthy", timestamp: new Date().toISOString() }));
      return;
    }

    if (pathname === "/api/v1/readiness" && method === "GET") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          ready: true,
          status: "ready",
          issues: [],
          details: { database: "connected", workers: 1 },
        })
      );
      return;
    }

    // Audit query
    if (pathname === "/api/v1/audit" && method === "GET") {
      assert.equal(reqUrl.searchParams.get("task_id"), taskId);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify([
          {
            id: auditId,
            event_type: "task_created",
            task_id: { id: taskId },
            timestamp: new Date().toISOString(),
            details: { priority: 5 },
            user_id: "eng-production",
          },
        ])
      );
      return;
    }

    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ detail: "Not found" }));
  });

  try {
    const client = new BabyClient({
      baseUrl: url,
      token: "test-operator-token",
      engagementId: "eng-production",
    });

    // 1. Submit task
    const submission = await client.tasks.submit({
      title: "Integration Test Task",
      description: "Testing worker pipeline",
      priority: 5,
    });
    assert.equal(submission.task_id, taskId);
    assert.equal(submission.status, TaskStatus.PENDING);
    assert.equal(submission.engagement_id, "eng-production");

    // 2. Retrieve task
    const task = await client.tasks.get(taskId);
    assert.equal(task.title, "Integration Test Task");
    assert.equal(task.status, TaskStatus.IN_PROGRESS);
    assert.equal(task.assigned_worker_id, "worker-test-01");

    // 3. Decide approval
    const approval = await client.tasks.decideApproval(taskId, approvalId, {
      approved: true,
      reason: "Security clearance granted",
    });
    assert.equal(approval.task_id, taskId);
    assert.equal(approval.approved, true);

    // 4. Cancel task
    const cancellation = await client.tasks.cancel(taskId);
    assert.equal(cancellation.task_id, taskId);
    assert.equal(cancellation.status, TaskStatus.CANCELLED);
    assert.equal(cancellation.cancel_requested, true);

    // 5. Worker query
    const workers = await client.workers.list();
    assert.equal(workers.length, 1);
    assert.equal(workers[0]?.worker_id, "worker-test-01");
    assert.equal(workers[0]?.status, WorkerStatus.BUSY);

    // 6. Observability
    const health = await client.observability.getHealth();
    assert.equal((health as { status: string }).status, "healthy");

    const readiness = await client.observability.getReadiness();
    assert.equal(readiness.ready, true);

    const auditEvents = await client.observability.listAuditEvents({ taskId });
    assert.equal(auditEvents.length, 1);
    assert.equal(auditEvents[0]?.event_type, AuditEventType.TASK_CREATED);
  } finally {
    await close();
  }
});

test("TypeScript client E2E: Security & Error hierarchies map accurately", async () => {
  let callCount = 0;
  const { url, close } = await createMockServer((req, res) => {
    callCount++;
    const path = req.url ?? "/";

    if (path.includes("/auth-401")) {
      res.writeHead(401, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ detail: "Missing or invalid token", code: "unauthenticated" }));
      return;
    }
    if (path.includes("/auth-403")) {
      res.writeHead(403, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ detail: "Admin privileges required", code: "unauthorized" }));
      return;
    }
    if (path.includes("/missing-404")) {
      res.writeHead(404, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ detail: "Task does not exist", code: "not_found" }));
      return;
    }
    if (path.includes("/conflict-409")) {
      res.writeHead(409, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ detail: "Task already in terminal state", code: "invalid_transition" }));
      return;
    }
    if (path.includes("/validation-422")) {
      res.writeHead(422, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          detail: [
            { loc: ["body", "title"], msg: "Field required", type: "missing" },
            { loc: ["body", "priority"], msg: "Input should be between 0 and 10", type: "value_error" },
          ],
          code: "validation_error",
        })
      );
      return;
    }
    if (path.includes("/server-500")) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ detail: "Database lock timeout", code: "database_error" }));
      return;
    }

    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ status: "ok" }));
  });

  try {
    const client = new BabyClient({ baseUrl: url });

    // 401
    await assert.rejects(
      () => client.transport.request("/auth-401"),
      (err: unknown) => {
        assert.ok(err instanceof AuthenticationError);
        assert.equal(err.statusCode, 401);
        assert.equal(err.code, "unauthenticated");
        return true;
      }
    );

    // 403
    await assert.rejects(
      () => client.transport.request("/auth-403"),
      (err: unknown) => {
        assert.ok(err instanceof AuthorizationError);
        assert.equal(err.statusCode, 403);
        assert.equal(err.code, "unauthorized");
        return true;
      }
    );

    // 404
    await assert.rejects(
      () => client.transport.request("/missing-404"),
      (err: unknown) => {
        assert.ok(err instanceof NotFoundError);
        assert.equal(err.statusCode, 404);
        assert.equal(err.code, "not_found");
        return true;
      }
    );

    // 409
    await assert.rejects(
      () => client.transport.request("/conflict-409"),
      (err: unknown) => {
        assert.ok(err instanceof ConflictError);
        assert.equal(err.statusCode, 409);
        assert.equal(err.code, "invalid_transition");
        return true;
      }
    );

    // 422
    await assert.rejects(
      () => client.transport.request("/validation-422"),
      (err: unknown) => {
        assert.ok(err instanceof ValidationError);
        assert.equal(err.statusCode, 422);
        assert.equal(err.validationIssues.length, 2);
        return true;
      }
    );

    // 500
    await assert.rejects(
      () => client.transport.request("/server-500"),
      (err: unknown) => {
        assert.ok(err instanceof ServerError);
        assert.equal(err.statusCode, 500);
        assert.equal(err.code, "database_error");
        return true;
      }
    );
  } finally {
    await close();
  }
});

test("TypeScript client E2E: Network failures, timeouts, and NO automatic retries on POST", async () => {
  let postAttempts = 0;

  const { url, close } = await createMockServer((req, res) => {
    if (req.method === "POST" && req.url === "/api/v1/tasks/submit") {
      postAttempts++;
      // Return 503 Service Unavailable to simulate transient server pressure
      res.writeHead(503, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ detail: "Worker pool busy", code: "service_unavailable" }));
      return;
    }
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({}));
  });

  try {
    const client = new BabyClient({ baseUrl: url, timeoutMs: 500 });

    // Verify non-retried state-changing call
    await assert.rejects(
      () => client.tasks.submit({ title: "No Retry Task" }),
      (err: unknown) => {
        assert.ok(err instanceof ServerError);
        assert.equal(err.statusCode, 503);
        return true;
      }
    );
    // Crucial guarantee: exact 1 attempt, ZERO automatic retries
    assert.equal(postAttempts, 1);

    // Network failure to closed port / invalid host
    const badClient = new BabyClient({ baseUrl: "http://127.0.0.1:1" });
    await assert.rejects(
      () => badClient.observability.getHealth(),
      (err: unknown) => {
        assert.ok(err instanceof NetworkError);
        return true;
      }
    );
  } finally {
    await close();
  }
});

test("TypeScript client E2E: Malformed JSON and unexpected non-JSON shapes", async () => {
  const { url, close } = await createMockServer((req, res) => {
    if (req.url === "/malformed") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end("{ invalid json syntax ");
      return;
    }
    if (req.url === "/plain-text") {
      res.writeHead(200, { "Content-Type": "text/plain" });
      res.end("gateway timeout");
      return;
    }
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({}));
  });

  try {
    const client = new BabyClient({ baseUrl: url });

    // Malformed JSON is caught safely and returned as null
    const malformedResult = await client.transport.request("/malformed");
    assert.equal(malformedResult, null);

    // Plain text is converted safely into detail object
    const textResult = (await client.transport.request("/plain-text")) as { detail: string };
    assert.equal(textResult.detail, "gateway timeout");
  } finally {
    await close();
  }
});
