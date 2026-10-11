import test from "node:test";
import assert from "node:assert/strict";
import {
  BabyClient,
  AuthenticationError,
  AuthorizationError,
  NotFoundError,
  ConflictError,
  ClientValidationError,
  ServerError,
  TimeoutError,
  NetworkError,
  TaskStatus,
  WorkerStatus,
} from "../dist/index.js";

test("BabyClient initializes with configured options", () => {
  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
    token: "test-token",
    engagementId: "eng-test",
  });

  assert.equal(client.transport.baseUrl, "http://localhost:8000");
  assert.ok(client.tasks);
  assert.ok(client.workers);
  assert.ok(client.observability);
});

test("HttpTransport strips trailing slashes from baseUrl", () => {
  const client = new BabyClient({
    baseUrl: "http://localhost:8000///",
  });
  assert.equal(client.transport.baseUrl, "http://localhost:8000");
});

test("HttpTransport correctly formats query parameters", () => {
  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
  });

  const url = client.transport.buildUrl("/api/v1/tasks", {
    status: TaskStatus.PENDING,
    limit: 20,
    offset: 0,
    empty: null,
    missing: undefined,
  });

  assert.equal(url, "http://localhost:8000/api/v1/tasks?status=pending&limit=20&offset=0");
});

test("HttpTransport passes Bearer token and Engagement headers", async () => {
  let capturedHeaders: HeadersInit | undefined;

  const mockFetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    capturedHeaders = init?.headers;
    return new Response(JSON.stringify([]), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
    token: "secret-token",
    engagementId: "eng-1",
    fetchFn: mockFetch,
  });

  await client.tasks.list();

  const headers = capturedHeaders as Record<string, string>;
  assert.equal(headers["Authorization"], "Bearer secret-token");
  assert.equal(headers["X-Engagement-ID"], "eng-1");
  assert.equal(headers["Accept"], "application/json");
});

test("HttpTransport passes X-API-Key when configured", async () => {
  let capturedHeaders: HeadersInit | undefined;

  const mockFetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    capturedHeaders = init?.headers;
    return new Response(JSON.stringify([]), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
    apiKey: "api-key-123",
    fetchFn: mockFetch,
  });

  await client.tasks.list();

  const headers = capturedHeaders as Record<string, string>;
  assert.equal(headers["X-API-Key"], "api-key-123");
});

test("HttpTransport throws AuthenticationError on 401 without leaking secrets", async () => {
  const mockFetch = (async () => {
    return new Response(JSON.stringify({ detail: "Authentication required" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
    token: "super-secret-password-123",
    fetchFn: mockFetch,
  });

  await assert.rejects(
    async () => {
      await client.tasks.list();
    },
    (err: unknown) => {
      assert.ok(err instanceof AuthenticationError);
      assert.equal(err.statusCode, 401);
      assert.equal(err.message, "Authentication required");
      // Verify token never appears anywhere in error message
      assert.ok(!JSON.stringify(err).includes("super-secret-password-123"));
      return true;
    }
  );
});

test("HttpTransport throws AuthorizationError on 403", async () => {
  const mockFetch = (async () => {
    return new Response(JSON.stringify({ detail: "Not authorized to access the requested engagement scope" }), {
      status: 403,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
    fetchFn: mockFetch,
  });

  await assert.rejects(
    async () => {
      await client.tasks.list({ engagementId: "forbidden-scope" });
    },
    (err: unknown) => {
      assert.ok(err instanceof AuthorizationError);
      assert.equal(err.statusCode, 403);
      assert.ok(err.message.includes("Not authorized"));
      return true;
    }
  );
});

test("HttpTransport throws NotFoundError on 404", async () => {
  const mockFetch = (async () => {
    return new Response(JSON.stringify({ detail: "Task not found" }), {
      status: 404,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
    fetchFn: mockFetch,
  });

  await assert.rejects(
    async () => {
      await client.tasks.get("00000000-0000-0000-0000-000000000000");
    },
    (err: unknown) => {
      assert.ok(err instanceof NotFoundError);
      assert.equal(err.statusCode, 404);
      return true;
    }
  );
});

test("HttpTransport throws ConflictError on 409", async () => {
  const mockFetch = (async () => {
    return new Response(JSON.stringify({ detail: "Invalid state transition" }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
    fetchFn: mockFetch,
  });

  await assert.rejects(
    async () => {
      await client.tasks.cancel("task-id");
    },
    (err: unknown) => {
      assert.ok(err instanceof ConflictError);
      assert.equal(err.statusCode, 409);
      assert.equal(err.message, "Invalid state transition");
      return true;
    }
  );
});

test("HttpTransport throws ClientValidationError on 422", async () => {
  const mockFetch = (async () => {
    return new Response(
      JSON.stringify({
        detail: [
          { loc: ["body", "title"], msg: "Field required", type: "missing" },
        ],
      }),
      {
        status: 422,
        headers: { "Content-Type": "application/json" },
      }
    );
  }) as typeof fetch;

  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
    fetchFn: mockFetch,
  });

  await assert.rejects(
    async () => {
      await client.tasks.submit({ title: "", description: "" });
    },
    (err: unknown) => {
      assert.ok(err instanceof ClientValidationError);
      assert.equal(err.statusCode, 422);
      assert.equal(err.validationIssues.length, 1);
      return true;
    }
  );
});

test("HttpTransport throws ServerError on 500", async () => {
  const mockFetch = (async () => {
    return new Response(JSON.stringify({ detail: "Internal server error" }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
    fetchFn: mockFetch,
  });

  await assert.rejects(
    async () => {
      await client.tasks.list();
    },
    (err: unknown) => {
      assert.ok(err instanceof ServerError);
      assert.equal(err.statusCode, 500);
      assert.equal(err.message, "Internal server error");
      return true;
    }
  );
});

test("HttpTransport respects request timeout and throws TimeoutError", async () => {
  const mockFetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    return new Promise((_, reject) => {
      init?.signal?.addEventListener("abort", () => {
        const error = new Error("The operation was aborted");
        error.name = "AbortError";
        reject(error);
      });
    });
  }) as typeof fetch;

  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
    timeoutMs: 50,
    fetchFn: mockFetch,
  });

  await assert.rejects(
    async () => {
      await client.tasks.list();
    },
    (err: unknown) => {
      assert.ok(err instanceof TimeoutError);
      assert.equal((err as TimeoutError).timeoutMs, 50);
      return true;
    }
  );
});

test("TasksApi correctly formats submit, get, cancel, decideApproval requests", async () => {
  const calls: { url: string; method: string; body?: unknown }[] = [];

  const mockFetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: String(input),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });

    if (String(input).includes("/submit")) {
      return new Response(JSON.stringify({ task_id: "t1", status: "pending", engagement_id: "eng-1" }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (String(input).includes("/cancel")) {
      return new Response(JSON.stringify({ task_id: "t1", status: "cancelled", cancel_requested: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    if (String(input).includes("/approvals/")) {
      return new Response(JSON.stringify({ task_id: "t1", status: "awaiting_approval", approved: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(JSON.stringify({ task_id: { id: "t1" }, title: "Task 1", status: "pending" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
    fetchFn: mockFetch,
  });

  const sub = await client.tasks.submit({ title: "Task 1", description: "desc", priority: 5 });
  assert.equal(sub.task_id, "t1");
  assert.equal(calls[0]?.method, "POST");
  assert.equal(calls[0]?.url, "http://localhost:8000/api/v1/tasks/submit");

  const getRes = await client.tasks.get("t1");
  assert.equal(getRes.title, "Task 1");
  assert.equal(calls[1]?.method, "GET");

  const cancelRes = await client.tasks.cancel("t1");
  assert.equal(cancelRes.status, "cancelled");
  assert.equal(calls[2]?.method, "POST");

  const approvalRes = await client.tasks.decideApproval("t1", "req-1", { approved: true, reason: "Approved by admin" });
  assert.equal(approvalRes.approved, true);
  assert.equal(calls[3]?.method, "POST");
  assert.equal(calls[3]?.url, "http://localhost:8000/api/v1/tasks/t1/approvals/req-1");
});

test("WorkersApi and ObservabilityApi execute correct endpoints", async () => {
  const requestedUrls: string[] = [];

  const mockFetch = (async (input: RequestInfo | URL) => {
    requestedUrls.push(String(input));
    return new Response(JSON.stringify({}), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;

  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
    fetchFn: mockFetch,
  });

  await client.workers.list({ status: WorkerStatus.IDLE });
  assert.equal(requestedUrls[0], "http://localhost:8000/api/v1/workers?status=idle");

  await client.workers.get("worker-1");
  assert.equal(requestedUrls[1], "http://localhost:8000/api/v1/workers/worker-1");

  await client.observability.getHealth();
  assert.equal(requestedUrls[2], "http://localhost:8000/api/v1/health");

  await client.observability.getReadiness();
  assert.equal(requestedUrls[3], "http://localhost:8000/api/v1/readiness");

  await client.observability.getMetrics();
  assert.equal(requestedUrls[4], "http://localhost:8000/api/v1/metrics");

  await client.observability.listAuditEvents({ eventType: "task_created" });
  assert.equal(requestedUrls[5], "http://localhost:8000/api/v1/audit?event_type=task_created");

  await client.observability.listAgents();
  assert.equal(requestedUrls[6], "http://localhost:8000/api/v1/agents");

  await client.observability.getAgent("agent-1");
  assert.equal(requestedUrls[7], "http://localhost:8000/api/v1/agents/agent-1");
});

// ---------------------------------------------------------------------------
// TasksApi.submit: optional Idempotency-Key
// ---------------------------------------------------------------------------

interface CapturedSubmit {
  url: string;
  method: string;
  headers: Record<string, string>;
  rawBody: string;
}

function captureSubmits(respond: () => Response): { calls: CapturedSubmit[]; fetchFn: typeof fetch } {
  const calls: CapturedSubmit[] = [];
  const fetchFn = (async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: String(input),
      method: init?.method ?? "GET",
      headers: { ...(init?.headers as Record<string, string>) },
      rawBody: String(init?.body),
    });
    return respond();
  }) as typeof fetch;
  return { calls, fetchFn };
}

const SUBMISSION = { title: "Report", description: "Summarise the repository", priority: 3 };
const IDEMPOTENCY_KEY = "4f9c2d1e-key-never-in-url-or-body";

function accepted(replayed: boolean): Response {
  return new Response(JSON.stringify({ task_id: "t1", status: "pending", engagement_id: "eng-1", replayed }), {
    status: 202,
    headers: { "Content-Type": "application/json", ...(replayed ? { "Idempotent-Replayed": "true" } : {}) },
  });
}

test("TasksApi.submit without options sends no Idempotency-Key and an unchanged body", async () => {
  const { calls, fetchFn } = captureSubmits(() => accepted(false));
  const client = new BabyClient({ baseUrl: "http://localhost:8000", token: "secret-token", fetchFn });

  const legacy = await client.tasks.submit(SUBMISSION);
  const emptyOptions = await client.tasks.submit(SUBMISSION, {});

  assert.equal(legacy.task_id, "t1");
  assert.equal(emptyOptions.replayed, false);
  for (const call of calls) {
    assert.equal(call.method, "POST");
    assert.equal(call.url, "http://localhost:8000/api/v1/tasks/submit");
    assert.equal("Idempotency-Key" in call.headers, false);
    assert.deepEqual(JSON.parse(call.rawBody), SUBMISSION);
  }
});

test("TasksApi.submit sends idempotencyKey only as the Idempotency-Key header", async () => {
  const { calls, fetchFn } = captureSubmits(() => accepted(false));
  const client = new BabyClient({
    baseUrl: "http://localhost:8000",
    token: "secret-token",
    engagementId: "eng-1",
    fetchFn,
  });

  await client.tasks.submit(SUBMISSION, { idempotencyKey: IDEMPOTENCY_KEY });

  const call = calls[0]!;
  assert.equal(call.headers["Idempotency-Key"], IDEMPOTENCY_KEY);
  // Authentication, engagement and content headers are unaffected.
  assert.equal(call.headers["Authorization"], "Bearer secret-token");
  assert.equal(call.headers["X-Engagement-ID"], "eng-1");
  assert.equal(call.headers["Content-Type"], "application/json");
  // The key never leaks into the URL or the body; the body is exactly the submission.
  assert.equal(call.url, "http://localhost:8000/api/v1/tasks/submit");
  assert.equal(call.rawBody.includes(IDEMPOTENCY_KEY), false);
  assert.deepEqual(JSON.parse(call.rawBody), SUBMISSION);
});

test("TasksApi.submit parses a replayed response with the same return type", async () => {
  const { fetchFn } = captureSubmits(() => accepted(true));
  const client = new BabyClient({ baseUrl: "http://localhost:8000", fetchFn });

  const replay = await client.tasks.submit(SUBMISSION, { idempotencyKey: IDEMPOTENCY_KEY });

  assert.deepEqual(replay, { task_id: "t1", status: "pending", engagement_id: "eng-1", replayed: true });
});

test("TasksApi.submit maps keyed errors as before and never exposes the key", async () => {
  const cases: [number, string, new (...args: never[]) => Error][] = [
    [409, "Idempotency-Key was already used for a different request", ConflictError],
    [422, "Idempotency-Key must be 1-255 visible ASCII characters", ClientValidationError],
    [503, "Task t1 was created but its audit record could not be written; retry the same request", ServerError],
  ];
  for (const [status, detail, errorClass] of cases) {
    const { calls, fetchFn } = captureSubmits(
      () => new Response(JSON.stringify({ detail }), { status, headers: { "Content-Type": "application/json" } })
    );
    const client = new BabyClient({ baseUrl: "http://localhost:8000", token: "secret-token", fetchFn });

    await assert.rejects(
      () => client.tasks.submit(SUBMISSION, { idempotencyKey: IDEMPOTENCY_KEY }),
      (err: unknown) => {
        assert.ok(err instanceof errorClass, `status ${status} should map to ${errorClass.name}`);
        const serialized = `${String((err as Error).message)} ${JSON.stringify(err)}`;
        assert.equal(serialized.includes(IDEMPOTENCY_KEY), false);
        return true;
      }
    );
    assert.equal(calls.length, 1, "a failed submission is never retried automatically");
  }
});
