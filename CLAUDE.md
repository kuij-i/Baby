# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Baby is a Python 3.10+ "personal AI operating system" that coordinates specialist AI agents under strict governance: every tool call goes through permission checks, approval gating, and audit logging. Only the **CodingAgent** is implemented today; trading/copywriting/testing/cybersecurity agents are planned. Trading is permanently **analysis-only** — no broker execution, exchange credentials, or order placement may be added.

## Commands

```bash
pip install -e ".[dev]"                 # install (editable) with dev tools
python -m pytest                        # all tests (asyncio_mode=auto, --strict-markers)
python -m pytest tests/test_coding_agent.py::TestClass::test_name   # single test
python -m pytest --cov=baby tests/      # what CI runs
ruff check baby tests                   # lint (CI)
mypy baby                               # type check (CI)
black baby tests && isort baby tests    # format (120-char lines; not enforced in CI)
uvicorn baby.api.app:app --reload       # run the observability API (never executes tasks)
python -m baby.workers run [--count N]  # run supervised worker processes (status | recover also available)
python scripts/generate_ts_types.py     # export OpenAPI schema & generate clients/typescript/src/types.ts
python scripts/generate_ts_types.py --check   # fail if docs/openapi.json or types.ts is stale (CI gate)
cd clients/typescript && npm ci && npm test   # build (pinned typescript) & run client tests; Node >= 22.6
```

CI (`.github/workflows/ci.yml`) runs on pushes/PRs to `main`: ruff → mypy → pytest with `--cov-fail-under=90` on Ubuntu (Python 3.10–3.13) and on Windows and macOS (3.10, 3.13); a contract job (`generate_ts_types.py --check`, then `npm ci && npm test` for the TypeScript client); a wheel build + clean-venv install smoke test; and `pip-audit` of runtime dependencies. Tests must not rewrite committed files (the contract test compares, it never writes). Markers `unit`, `integration`, `slow` are registered; unregistered markers fail under `--strict-markers`. Prefer `python -m pytest` so tests run with the interpreter that has the package installed.

## Architecture

Commit messages (`PHASE N: ...`) record what each development phase added; there is no separate design-doc folder, so the code and this file are the reference.

### Execution flow
Production path: `POST /tasks/submit` (or `DurableTaskQueue.submit`) writes a PENDING row → a worker process claims it → `Orchestrator.execute_claimed_task` → agent → tools → fenced result write (see *Durable workers*). In-process path (tests, embedding): `orchestration.Orchestrator.execute_task` → `planning.planner.TaskPlanner` (currently produces a single step; `approval_required` when priority ≥ 8) → `planning.selection` picks an enabled agent from `agents.registry` → `Agent.execute(ExecutionContext)` → agent calls tools **only via `tools.executor.ToolExecutor`** → result. Task state is tracked by `observability.tasks.TaskTracker`, and every stage records an `AuditEvent`.

### Security invariants (fail-closed — preserve these)
- `ToolExecutor.execute` validates input, checks each `tool.permissions_required` against `permissions.PermissionManager`, **raises `ApprovalRequiredError` for `APPROVAL_REQUIRED`** unless `approval_service` consumes an admin-granted, single-use approval bound to that exact task/engagement/operation (no implicit approval; workers cannot grant) and `PermissionDeniedError` for denied/missing permissions, audits invocation/result, and enforces `spec.timeout_seconds`. Never execute tools directly, bypassing the executor.
- No shell/subprocess execution anywhere. Coding tools (`tools/coding/`: list/read/write) are confined to `settings.coding_repo_root` via `path_resolver` + `CodingFilesystem`; path traversal raises `PathTraversalError`, any `.git` access raises `RepositoryBoundaryError`. Before coding tools are enabled, `path_resolver.validate_coding_repo_root` (called by `workers.bootstrap.default_bootstrap` and by `WorkerSupervisor.start` for the default bootstrap) requires the root to exist and rejects roots inside `.git` or containing Baby's database or `.env` (`LOCAL_READ` needs no approval) → `ConfigurationError`, worker exits with the config code. `CodingFilesystem` is the abstract boundary (a native/Rust engine may replace `LocalCodingFilesystem` later) — keep agents and policies coded against the interface.
- CodingAgent: `LOCAL_READ` = ALLOW, `LOCAL_WRITE` = APPROVAL_REQUIRED. Its model loop (`_execute_with_provider`) is bounded by `coding_max_iterations` / `coding_max_tool_calls_per_iteration` and fails the step (not silently skips) on unknown tools, malformed args, or exceeding limits. Providers are optional: `workers.bootstrap.default_bootstrap` calls `providers.registry.register_configured_providers()` (NvidiaProvider when `NVIDIA_API_KEY` is set, else OpenAIProvider when `OPENAI_API_KEY` is set; see *Providers*). Without a provider only explicit tool steps and whole-word "list"/"ls" requests run; every other step returns `success=False` ("No model provider is configured") — never report a step completed when nothing ran.
- Audit details and logs must never contain secrets; `observability/redaction.py` exists for this (key matching is by substring; token *counters* such as `tokens_used` are exempt so typed fields keep their type).
- Logging: `baby.logging.configure_logging(settings.log_level)` runs in the API lifespan and in every worker process (after forwarded settings are applied); it renders each event once to the current `sys.stderr`.

### Durable workers (`baby/workers/`, Phase 12)
Tasks execute only in separate OS processes; SQLite is the single source of truth and the only coordination channel (no Redis/Celery/etc.).

```
API ──insert PENDING──▶ SQLite ◀──claim / renew / fenced write── worker process (spawned) ×N
                          ▲                                         │ heartbeat thread
                          └──── recovery (API startup, supervisor, workers) ◀┘
```

- **Queue** (`queue.DurableTaskQueue`): no queue table — a task is queued when its `task_records` row is PENDING, or AWAITING_APPROVAL with every approval decided and an unconsumed grant. `claim_next` runs in one `BEGIN IMMEDIATE` transaction (`DatabaseManager.immediate_transaction`): pick by priority/age, validate via `ensure_valid_transition`, bump `claim_count`, insert a `task_leases` row (PK = task_id). Only a registered live worker can claim.
- **Leases/fencing**: lease = `(worker_id, fence=claim_count, lease_expires_at)`. `renew`, `finalize` and `park_for_approval` must present the worker's fence and an unexpired lease, else `LeaseLostError` and nothing is written. A worker never writes a result for a task it no longer owns.
- **Heartbeats/status**: `workers` row per process identity (`worker-<uuid>`, never reused). Status STARTING → IDLE ⇄ BUSY → STOPPING → STOPPED; recovery sets STALE (heartbeat older than `worker_stale_timeout_seconds`) or ERROR (process exited). A STALE/ERROR identity cannot heartbeat, claim, renew or finalize — the worker must exit.
- **Recovery** (`recovery.WorkerRecovery`, idempotent): marks stale workers; revokes leases that expired or whose worker is not live; the task goes IN_PROGRESS → FAILED with an explicit reason. **Interrupted work is never re-queued or replayed.** API startup runs lease recovery, then `task_tracker.recover_stale_tasks()` which fails only IN_PROGRESS tasks *without* a lease — tasks owned by live workers survive API restarts.
- **Runtime** (`runtime.WorkerRuntime`): startup = register → recovery → heartbeat thread → IDLE → poll/claim. While a task runs, a watchdog renews the lease each heartbeat interval and cancels execution on lease loss/DB failure (no write), cancel request (CANCELLED), `worker_max_task_seconds` (FAILED) or shutdown (FAILED, "shut down … not replayed"). A failed result with an undecided approval request parks the task (IN_PROGRESS → AWAITING_APPROVAL, lease released).
- **Processes** (`process.py`, `supervisor.py`): always `multiprocessing.get_context("spawn")` (Windows-safe; no inherited connections). `run_worker_process` is top-level/picklable; `BABY_WORKER_PROCESS=1` prevents workers from starting supervisors; `__main__` is guarded. Only `FORWARDABLE_SETTINGS` (non-secret) cross the process boundary; secrets come from env. The supervisor recovers exited workers immediately, restarts them with new identities (not on config-error exit code 2), and on stop signals → waits `worker_shutdown_grace_seconds` → terminates.
- **Bootstrap**: worker processes start with empty registries; `bootstrap.default_bootstrap` registers only the CodingAgent with exactly its declared permissions. Tests use `tests/worker_helpers.py` (spawned children need importable helpers).
- **Approvals** (`permissions/approvals.ApprovalService`): `ToolExecutor` (APPROVAL_REQUIRED) and `execute_claimed_task` (`PlanStep.approval_required`, priority ≥ 8) call `authorize_or_request`. A grant is bound to task + engagement + SHA-256 of (operation, agent, permission, arguments), requires the task to be IN_PROGRESS, and is consumed atomically (single use) — a crash can never replay an approved write. Only `decide()` (admin-only `POST /tasks/{id}/approvals/{request_id}`) can approve/deny; nothing in `baby/workers/` calls it (enforced by a test). Denial → FAILED. Approvals expire after `approval_ttl_seconds` (undecided: from `created_at`; granted-but-unused: from `approval_timestamp`): `decide()` on an expired request fails the task and raises `ApprovalExpiredError` (API 409), `approvals_ready` never resumes such a task, an expired grant is never consumed, and `WorkerRecovery.recover()` fails unleased expired AWAITING_APPROVAL tasks (audit TASK_FAILED, reason `approval_expired`). A resumed task re-runs from the start (no step checkpoints yet), so the runtime fails a task closed once it would pause for more than `max_approval_rounds` approvals.
- **Audit/metrics**: `audit_log.use_backend(SQLiteAuditLog(...))` is attached by the API lifespan and every worker so worker events (WORKER_STARTED/STOPPED/STALE/HEARTBEAT_FAILED, TASK_CLAIMED/LEASE_RENEWED/LEASE_LOST/RECOVERED/CANCELLED) are durable and visible via `/audit`. Metric labels never contain task or worker IDs.

### Core contracts
All cross-component data is Pydantic models in `baby/core/contracts.py` (exported from `baby/core/__init__.py`): `Task`, `Plan`/`PlanStep`, `AgentSpec`, `ToolPermission`, `AgentResult`, `AuditEvent`, `TaskRecord`, `VALID_TASK_TRANSITIONS`, etc. Timestamps are **naive UTC** everywhere (storage, comparisons, API): use `core.utc_now()` (never `datetime.utcnow()`, deprecated) and type datetime fields as `UTCDatetime`, which converts aware input to naive UTC; use `core.to_naive_utc` for other inputs (e.g. query parameters). Pydantic models use `ConfigDict`, not class-based `Config`. Errors live in `baby/errors.py`.

### Global singletons
Most subsystems are module-level singletons imported directly: `settings`, `audit_log`, `permission_manager`, `agent_registry`, `tool_registry`, `tool_executor`, `provider_registry`, `task_planner`, `selection_logic`, `orchestrator`, `task_tracker`, `worker_tracker`, `metrics_collector`, `health_checker`, `memory_store`, `db_manager`, `approval_service`. Singletons are per process — a worker process shares nothing with the API except the database. Worker tests isolate the DB with `db_manager.configure(f"sqlite:///{tmp_path/'baby.db'}")` (see `tests/test_workers.py::durable_env`). Tests reset them with autouse fixtures (e.g. `task_tracker.clear()`) and toggle config with `patch("baby.configuration.settings.<field>", ...)` or `patch.object(settings, ...)` — follow the same pattern. Watch for circular imports between `planning` and `orchestration` (previously fixed; `api/app.py` imports lazily inside `lifespan` for this reason).

### Task lifecycle
`VALID_TASK_TRANSITIONS` + `core.ensure_valid_transition` is the only state machine; `TaskTracker`, the worker queue, recovery and the approval service all call it (invalid → `InvalidStateTransitionError`; COMPLETED/FAILED/CANCELLED are terminal). Tasks must be registered PENDING, then moved to IN_PROGRESS. With SQLite configured, `TaskTracker` is store-first: every read hits the DB and every mutation is an atomic read-modify-write (`SQLiteTaskStore.update`), so the API sees worker updates; a failed durable write raises (fail closed). The in-memory dict is used only for `persistence_backend='in_memory'`. On API startup, lease recovery runs and `recover_stale_tasks()` marks unleased IN_PROGRESS tasks FAILED.

### Persistence
`persistence/database.DatabaseManager` (SQLite, WAL, transactional; `db_manager` honors `settings.database_url`) backs `SQLiteTaskStore`, `SQLiteWorkerStore`, `SQLiteAuditLog`, `SQLiteMemoryStore`. Use `immediate_transaction()` for any read-modify-write another process may race on. Schema version lives in `PRAGMA user_version` (2 = Phase 12: `workers`, `task_leases`, `task_records.cancel_requested/claim_count`; 3 = Phase 17: `task_idempotency_keys`); `init_schema()` migrates older DBs in place with `ALTER TABLE ADD COLUMN` and never rewrites data. in-memory equivalents implement the same interfaces (`memory.base.MemoryStore`, `audit.AuditLog`). Selected by `persistence_backend` / `memory_storage_type` / `audit_storage_type`. Misconfigured SQLite is a startup `ConfigurationError` — never silently fall back to memory in production.

### Providers
`providers/base.ModelProvider` abstraction + `provider_registry`; `OpenAIProvider` is OpenAI-compatible (`openai_base_url` allows local endpoints). `NvidiaProvider` subclasses it for the NVIDIA API Catalog / NIM (`nvidia_api_key`, `nvidia_base_url`, `nvidia_model`). `registry.active_provider_name()` picks one from `model_provider` (`auto` = NVIDIA if its key is set, else OpenAI); `register_configured_providers()` registers only that one and `configured_model()` is the model the CodingAgent spec requests. Tool-role messages are sent without `name` (strict servers reject it). `is_available()` must return False without credentials.

### Observability API (`baby/api/`)
FastAPI app that is **read-only except task control**: `ReadOnlyMiddleware` returns 405 for non-GET/HEAD/OPTIONS unless the request matches `TASK_CONTROL_ENDPOINTS` — `POST /tasks/submit` (operator/admin; engagement resolved server-side; optional `Idempotency-Key` handled by `DurableTaskQueue.submit_idempotent`: one `BEGIN IMMEDIATE` transaction writes the task and its `task_idempotency_keys` row together, scoped to (principal = `caller_id`, engagement, SHA-256 of the key); same request → existing task with `replayed=true`, different request → 409; `TASK_CREATED` is written after commit with a deterministic id via `audit_log.record_once`, so a failed write returns 503 and a keyed replay records it exactly once — never write it with plain `record()`), `POST /tasks/{id}/cancel` (engagement-scoped), `POST /tasks/{id}/approvals/{request_id}` (admin only). These only write durable task state; the API never executes tasks (`POST /tasks` itself stays 405). `GET /` is a static landing response (not in the OpenAPI contract). The API registers no agents, so `/health`'s agents component and `/readiness` count live durable worker processes (`observability.health.count_live_durable_workers`). `/workers` merges in-process and durable workers; non-admins get no host/pid metadata and see `current_task_id` only for their own engagement. Every router is mounted twice — at the root and under `/api/v1` — so new routes must be added to both. Auth (`api/auth.py`): bearer tokens (`api_auth_token` = operator, `api_admin_token` = admin). The `X-Engagement-ID` header only *selects* among engagements configured server-side in `operator_engagements`; it must never expand access. `api_require_auth=False` is dev-only (everyone becomes admin) and is accepted only with `development_mode=True`; without it startup fails and `get_current_caller` keeps authentication enforced. Credential policy (`configuration.api_token_problem`, enforced by `validate_settings` and again at request time): tokens ≥ 32 chars, no placeholders, no surrounding whitespace, operator ≠ admin. Token checks compare UTF-8 bytes with `secrets.compare_digest` (non-ASCII input → 401, not 500). `api/openapi.export_openapi_schema` produces the contract for the TypeScript client.

### Typed API Contract & TypeScript Client (`clients/typescript/`, Phase 13)
- **Authoritative backend & contract**: FastAPI generates OpenAPI 3.1.0 (`docs/openapi.json`). Python models in `baby/api/schemas.py` define wire shapes (`TaskView`, `WorkerView`, etc.) separated from domain internals.
- **Deterministic TypeScript generation**: `scripts/generate_ts_types.py` uses pure standard-library Python to parse `docs/openapi.json` into typed interfaces and enums in `clients/typescript/src/types.ts`.
- **Client library**: `@baby/client` provides `BabyClient` (with `TasksApi` — `submit(submission, { idempotencyKey? })` — `WorkersApi`, `ObservabilityApi`), `HttpTransport` with configurable timeout/AbortSignal, engagement header injection, and a structured error hierarchy (`BabyClientError`, `AuthenticationError`, `AuthorizationError`, `NotFoundError`, `ConflictError`, `ValidationError`, `ServerError`). Zero external runtime dependencies; no Python or database imports allowed.
- **Fail-closed security**: No retries on non-idempotent writes; secret tokens redacted; permissions and engagement boundaries remain strictly enforced server-side.

### End-to-End Integration & Reliability (`tests/test_e2e_integration.py`, Phase 14)
- **Full Execution Path**: Verified `TypeScript client → /api/v1 HTTP API → SQLite queue → isolated worker processes → permissions & approvals → SQLite results → audit trail → typed response`.
- **Scenarios verified**:
  - Scenario A: Task submission (API non-executing) → worker execution → result retrieval → audit & observability.
  - Scenario B: Authentication rejection (401), IDOR isolation (404), engagement scoping (403 on expansion), admin-only approvals (403 on non-admin), worker metadata sanitization.
  - Scenario C: Approval pausing (`AWAITING_APPROVAL`), admin denial (`FAILED`, fail-closed), single-use grant execution (`COMPLETED`), replay blocking, operation digest binding.
  - Scenario D: Cancellation across states (PENDING immediately cancelled, IN_PROGRESS cancelled via watchdog, AWAITING_APPROVAL cancelled, 409 on terminal, late worker writes rejected).
  - Scenario E: Worker & database failures (API restart preserves live leased tasks, worker crashes fail-closed with "not replayed", expired leases reject writes, concurrency race has single winner, graceful shutdown).
  - Scenario F: TypeScript client integration (`clients/typescript/tests/integration.test.ts`) against HTTP server verifying full round-trips, typed error mappings, network errors, timeouts, malformed JSON resilience, zero retries on POST.
### Installation & Deployment Experience (Phase 15)
- **Local & Self-Contained**: Baby runs entirely on local SQLite (WAL mode) with no external message broker, background daemon, or cloud service dependencies.
- **Fail-Closed DB Initialization**: `DatabaseManager` creates directories automatically if writable; uncreatable/unwritable directories fail closed with clear, actionable `ConfigurationError`.
- **Environment Template**: `.env.example` provides an exhaustive reference covering log levels, auth tokens, model providers, SQLite URLs, worker supervisor tuning, and coding agent sandbox paths.
- **CLI Utilities**: Supervised worker processes run via `python -m baby.workers run [--count N]`, inspection via `python -m baby.workers status`, and crashed worker recovery via `python -m baby.workers recover`.

### Security & Operational Hardening (Phase 16)
- **Constant-Time Auth Verification**: Token checks in `baby/api/auth.py` use `secrets.compare_digest` to prevent timing side-channel attacks on `api_auth_token` and `api_admin_token`.
- **Worker Visibility Sanitization**: In-process workers (`worker_tracker`) now share the same host/process metadata and task-scoping protections as durable workers for non-admin callers.
- **Expanded Secret Redaction**: String-level secret scrubbing in `baby/observability/redaction.py` catches Anthropic (`sk-ant-`) and NVIDIA (`nvapi-`) keys in addition to OpenAI, Bearer tokens, and GitHub personal access tokens.

## Configuration
`baby/configuration.Settings` (pydantic-settings, reads `.env`, case-insensitive). `validate_settings()` raises on critical misconfig and returns warnings otherwise. Copy `.env.example` to `.env`; secrets (`OPENAI_API_KEY`, API tokens) only come from env.

## Conventions
- Black/ruff/isort at 120 columns; type hints on public functions; Google-style docstrings.
- New contract: add to `core/contracts.py`, export from `core/__init__.py`, test in `tests/test_contracts.py`.
- New tools: subclass `tools.base.Tool`, declare `permissions_required`, register, and test the security constraints (denial/approval paths), not just the happy path.
