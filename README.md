# Baby

Baby is a personal AI operating system / AI control center designed to coordinate multiple specialist AI agents across different domains (coding, trading, copywriting, testing, cybersecurity) with strong governance, permission controls, and audit logging.

## Architecture

Baby follows a layered orchestration architecture:

```
USER TASK
↓
BABY ORCHESTRATOR
↓
PLANNER
↓
ROUTER
↓
SPECIALIST AGENT
↓
TOOLS
↓
VERIFICATION
↓
MEMORY
↓
AUDIT LOG
↓
FINAL RESPONSE
```

### Durable workers and process isolation (Phase 12)

Tasks never execute inside the API process. The API (or any client) enqueues a task
durably; separate worker **processes** claim it from SQLite under a fenced lease and run
the agent logic.

```
                    ┌───────────────────┐
                    │    FastAPI API    │  POST /tasks/submit · GET /tasks/{id} · GET /workers
                    └─────────┬─────────┘
                              │ insert PENDING row (no execution)
                              ▼
                    ┌───────────────────┐
                    │  SQLite (WAL)     │  task_records · task_leases · workers · audit_events
                    └─────────┬─────────┘
                              │ atomic claim (BEGIN IMMEDIATE) → lease + fence
             ┌────────────────┴────────────────┐
             ▼                                 ▼
     ┌─────────────────┐             ┌─────────────────┐
     │ Worker process 1│             │ Worker process 2│   heartbeat thread + lease watchdog
     │  (spawned)      │             │  (spawned)      │
     └────────┬────────┘             └────────┬────────┘
              ▼                               ▼
     Orchestrator → Agent → ToolExecutor (permissions · approvals · audit)
              │                               │
              └──────── fenced result write ──┘ → SQLite
```

```bash
python -m baby.workers run --count 2     # start a supervised pool of worker processes
python -m baby.workers status            # list registered workers
python -m baby.workers recover           # fail closed any work whose worker/lease was lost
```

Or set `WORKER_ENABLED=true` to have the API start and stop the worker processes itself.
A crashed or stale worker's task is marked **FAILED** with an explicit reason; it is
**never replayed automatically**, because interrupted work may already have performed
privileged or side-effecting operations. See `CLAUDE.md` → *Durable workers* for the full
lifecycle, lease, heartbeat, recovery and approval semantics.

| Setting | Default | Bounds | Meaning |
|---|---|---|---|
| `WORKER_ENABLED` | `false` | — | API lifespan supervises worker processes |
| `WORKER_COUNT` | `1` | 1–32 | worker processes per supervisor |
| `WORKER_POLL_INTERVAL_SECONDS` | `1.0` | 0.01–60 | idle wait between claim attempts |
| `WORKER_HEARTBEAT_INTERVAL_SECONDS` | `5.0` | 0.01–300 | heartbeat and lease-renewal cadence |
| `WORKER_STALE_TIMEOUT_SECONDS` | `30.0` | ≥ 2 × heartbeat | heartbeat age after which a worker is STALE |
| `TASK_LEASE_DURATION_SECONDS` | `60.0` | ≥ 2 × heartbeat | un-renewed lease lifetime |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | `10.0` | 0–600 | wait before terminating a stopping worker |
| `WORKER_MAX_TASK_SECONDS` | `3600` | 1–86400 | bounded execution per claimed task |

Invalid values raise `ConfigurationError` at startup. Defaults suit a single low-resource machine.

## Core Principles

- **Production-quality code** with strong typing
- **Clear separation of concerns** with modular architecture
- **Security by default** with permission controls and approval workflows
- **Auditability** of all meaningful actions
- **Extensibility** to support new agent types and tools
- **No unrestricted autonomous execution** - all actions have boundaries

## Project Structure

```
baby/
  core/                  # Pydantic domain contracts (contracts.py)
  orchestration/         # Orchestrator: plan → select agent → execute → audit
  planning/              # TaskPlanner and agent selection logic
  agents/                # Agent base class, registry, specialist agents
    coding/              # CodingAgent (repository-bounded)
  tools/                 # Tool base, registry, permission-enforcing executor
    coding/              # list_files / read_file / write_file + CodingFilesystem
  permissions/           # Permission manager
  providers/             # Model provider abstraction (OpenAI-compatible)
  memory/                # MemoryStore interface + in-memory implementation
  audit/                 # Audit log
  persistence/           # SQLite database manager and durable stores (tasks, workers, audit, memory)
  workers/               # Durable task queue, leases, worker runtime, recovery, supervisor
  observability/         # Task/worker tracking, metrics, health, redaction
  api/                   # FastAPI observability API (+ /api/v1); read-only except task control
  configuration.py       # Settings (pydantic-settings, .env)
  logging.py             # Structured logging
  errors.py              # Exception types

tests/                   # Test suite
.github/                 # CI workflow
```

## Installation & Deployment (Phase 15)

Baby is designed for local, self-contained deployment with zero external broker or database daemon dependencies. The single source of truth for task queueing, state transitions, leases, audit logs, and agent memory is local SQLite with Write-Ahead Logging (WAL).

### System Prerequisites
- **Python**: 3.10, 3.11, or 3.12 (standard library includes `sqlite3`)
- **Node.js** (Optional, for TypeScript client): Node >= 20.0 and npm >= 9.0 to use it; Node >= 22.6 to run its tests
- **Operating Systems Supported**: Windows (10/11/Server), Linux (Ubuntu, Debian, Fedora, Arch), macOS (12+)
- **External Dependencies**: None required (No Docker, Redis, Celery, PostgreSQL, or cloud infrastructure needed)

---

### Step-by-Step Installation

#### Option A: Windows (PowerShell)

```powershell
# 1. Clone repository
git clone https://github.com/kuij-i/Baby.git
cd Baby

# 2. Create virtual environment
python -m venv .venv

# 3. Activate virtual environment
.\.venv\Scripts\Activate.ps1

# 4. Install Baby and dependencies
python -m pip install --upgrade pip
pip install -e ".[dev]"

# 5. Configure environment
Copy-Item .env.example .env
```

#### Option B: Linux & macOS (bash / zsh)

```bash
# 1. Clone repository
git clone https://github.com/kuij-i/Baby.git
cd Baby

# 2. Create virtual environment
python3 -m venv .venv

# 3. Activate virtual environment
source .venv/bin/activate

# 4. Install Baby and dependencies
python3 -m pip install --upgrade pip
pip install -e ".[dev]"

# 5. Configure environment
cp .env.example .env
```

---

### Configuration & Authentication

Open `.env` in your text editor to configure settings:

1. **Authentication (enforced by default)**:
   `API_REQUIRE_AUTH=true` is the default. `.env.example` ships empty tokens; generate a separate secret for each:
   ```bash
   python -c "import secrets; print('API_AUTH_TOKEN=' + secrets.token_hex(24))"
   python -c "import secrets; print('API_ADMIN_TOKEN=' + secrets.token_hex(24))"
   ```
   Startup fails closed (`ConfigurationError`) if a token is a placeholder (e.g. contains `change-me`), is shorter
   than 32 characters, has surrounding whitespace, or if the operator and admin tokens are identical.
   Disabling authentication (`API_REQUIRE_AUTH=false`) is only accepted together with `DEVELOPMENT_MODE=true`;
   that gives every caller admin access, so use it only locally with the API bound to `127.0.0.1`.
2. **Engagements**:
   Operators are scoped to specific engagement IDs (e.g., `OPERATOR_ENGAGEMENTS=default,engagement-1`). Requests can pass `X-Engagement-ID` to choose an active engagement.
3. **Coding repository root** (required by workers):
   Set `CODING_REPO_ROOT` to the project directory the CodingAgent may read (writes need admin approval).
   `python -m baby.workers run` refuses to start (exit code 2) if it is unset, missing, not a directory, inside a
   `.git` directory, or contains Baby's database or `.env` file. Use a directory beside Baby's data, not above it.
4. **Database URL**:
   Defaults to `sqlite:///baby.db`. Parent directories are automatically created on first boot. The database automatically initializes in WAL mode with busy timeout handling.

---

### Starting the Services

Baby strictly isolates API request handling from worker execution. Tasks submitted to the API are recorded durably as `PENDING` in SQLite; worker processes claim and execute them.

#### Mode 1: Split Architecture (Recommended for Production)

Run the API and worker pool in separate terminal windows:

**Terminal 1 — HTTP Observability & Task Submission API:**
```bash
# Windows / Linux / macOS
uvicorn baby.api.app:app --host 127.0.0.1 --port 8000
```

**Terminal 2 — Isolated Worker Supervisor Pool:**
```bash
# Windows / Linux / macOS
python -m baby.workers run --count 2
```

#### Mode 2: Unified Process Supervision (Convenient for Dev / Single-Command)

Enable `WORKER_ENABLED=true` in your `.env` or pass via environment variable. The FastAPI lifespan will supervise worker child processes automatically:

```bash
WORKER_ENABLED=true uvicorn baby.api.app:app --host 127.0.0.1 --port 8000
```

---

### First-Run Verification Walkthrough

Once running, verify your installation using `curl`:

1. **Check System Health:**
   ```bash
   curl -s http://127.0.0.1:8000/api/v1/health
   # Response: {"status":"healthy","version":"0.1.0","checks":{...}}
   ```

2. **Check System Readiness:**
   ```bash
   curl -s http://127.0.0.1:8000/api/v1/readiness
   # Response: {"status":"ready","database_connected":true,...}
   ```

3. **Check Worker Status:**
   ```bash
   python -m baby.workers status
   # Or via API:
   curl -s http://127.0.0.1:8000/api/v1/workers
   ```

4. **Submit a Safe Task:**
   ```bash
   curl -s -X POST http://127.0.0.1:8000/api/v1/tasks/submit \
     -H "Content-Type: application/json" \
     -d '{"description":"Test first-run verification","priority":5}'
   # Response: {"task_id":"task-...","status":"PENDING",...}
   ```

5. **Track Task Completion:**
   ```bash
   curl -s http://127.0.0.1:8000/api/v1/tasks/<task_id>
   ```

---

### Graceful Shutdown, Recovery & Backups

#### Graceful Shutdown
- Press `Ctrl+C` (SIGINT) or send `SIGTERM` to the API or worker process.
- The supervisor notifies workers, waits up to `WORKER_SHUTDOWN_GRACE_SECONDS` (default 10s) for in-flight tasks to finish or release leases, and exits cleanly.

#### Crash Recovery
- If a worker process crashes abruptly or is killed (`SIGKILL`), the task is **never replayed** (fail-closed security).
- To recover orphaned or stale task leases:
  ```bash
  python -m baby.workers recover
  ```
- Orphaned tasks are moved to `FAILED` with an audit event stating the failure reason.

#### Backup and Restore
Baby uses SQLite with WAL mode (`baby.db`, `baby.db-wal`, `baby.db-shm`).
- **Online Backup (Safe while running):**
  Use SQLite's native online backup API:
  ```bash
  sqlite3 baby.db ".backup 'baby-backup.db'"
  ```
  Or via Python script:
  ```python
  import sqlite3
  src = sqlite3.connect("baby.db")
  dst = sqlite3.connect("backup.db")
  src.backup(dst)
  src.close()
  dst.close()
  ```
- **Restore:**
  Stop the API and workers, copy `backup.db` over `baby.db`, and delete leftover `-wal` and `-shm` files before restarting.

---

### Troubleshooting Common First-Run Issues

| Symptom | Cause | Solution |
|---|---|---|
| `ConfigurationError: Database directory ... does not exist and could not be created` | Target directory has insufficient OS filesystem permissions or invalid path syntax. | Check `DATABASE_URL` in `.env`. Ensure your user has write permissions to the directory. |
| `ConfigurationError: Worker stale timeout (...) must be at least 2x heartbeat (...)` | Configuration timing bounds violation. | Ensure `WORKER_STALE_TIMEOUT_SECONDS >= 2 * WORKER_HEARTBEAT_INTERVAL_SECONDS`. |
| `HTTP 401 Unauthorized` | `API_REQUIRE_AUTH=true` is set, but token was missing or invalid. | Supply `-H "Authorization: Bearer <API_AUTH_TOKEN>"`. For local-only dev, set both `API_REQUIRE_AUTH=false` and `DEVELOPMENT_MODE=true`. |
| `ConfigurationError: api_... is a placeholder value` / `must be at least 32 characters` / `must be different` | API token fails the credential policy. | Generate a unique token per role with `python -c "import secrets; print(secrets.token_hex(24))"`. |
| `Configuration error: Coding repository root ...` (workers exit 2) | `CODING_REPO_ROOT` unset, invalid, or contains Baby's database/`.env`. | Point `CODING_REPO_ROOT` at a separate project directory. |
| `HTTP 403 Forbidden` | Operator attempted admin action (e.g. approval) or used an unpermitted engagement. | Supply the `API_ADMIN_TOKEN` or verify `OPERATOR_ENGAGEMENTS` in `.env`. |
| `HTTP 405 Method Not Allowed` on `/tasks` | Direct `POST /tasks` is intentionally disabled to prevent bypass of queue. | Submit tasks to `/api/v1/tasks/submit`. |
| Port 8000 already in use | Another service is using port 8000. | Run with `--port 8080` (or another free port). |
| `OperationalError: database is locked` | High concurrency without WAL mode. | Baby enforces WAL mode by default; ensure your filesystem supports POSIX locks (avoid remote SMB mounts for the SQLite DB). |

---

### Model Provider (optional)

Set `OPENAI_API_KEY` (and optionally `OPENAI_BASE_URL` / `DEFAULT_MODEL` for OpenAI-compatible
NVIDIA or local endpoints) to let workers run the CodingAgent's bounded model loop. Without a key,
workers only run explicit tool steps and "list files" requests; any other task is recorded as
**FAILED** with `No model provider is configured` — Baby never reports work as completed when
nothing was executed.

### Idempotent Task Submission

`POST /api/v1/tasks/submit` accepts an optional `Idempotency-Key` header (1–255 visible ASCII
characters, e.g. a UUID) so clients can retry safely:

- **Scope:** the key is scoped to the authenticated principal (operator, admin or local-dev
  caller) **and** the resolved engagement. The same key used by another principal or in another
  engagement is unrelated: it never returns someone else's task.
- **Same key, same request** (same title, description, priority and engagement): the original
  task is returned with `"replayed": true` and an `Idempotent-Replayed: true` header; no
  duplicate task is created. `status` is the task's current status.
- **Same key, different request:** `409 Conflict`; nothing is created.
- **Invalid key** (empty, too long, spaces or non-ASCII): `422`.
- **Guarantees:** the task and its key are written in one SQLite transaction, so concurrent
  retries (from any process) create exactly one task, a failed request never reserves the key,
  and replays work across API and worker restarts.
- **Retention:** keys are kept as long as the task record exists (there is no expiry). Only a
  SHA-256 of the key and of the request fields is stored.
- **Creation audit:** every created task gets exactly one `TASK_CREATED` audit event. The task is
  committed before that event is written, so if the audit write fails the request returns
  `503` naming the task: it was created, but the API does not report success. Retrying with the
  same key returns that task (`replayed: true`) and records the missing event (marked
  `recovered_on_replay`); retries never create a second task or a second event. Without a key
  there is nothing to retry against, so the `503` says not to resubmit.
- Requests without the header behave exactly as before (apart from that `503` on an audit failure).
- **TypeScript client:** `client.tasks.submit(submission, { idempotencyKey })`; the option is
  optional and is sent only as the header.

### Approval Lifecycle

- High-risk operations (file writes, priority ≥ 8 plan steps) pause the task in `AWAITING_APPROVAL`
  until an admin decides via `POST /api/v1/tasks/{task_id}/approvals/{request_id}`.
- Requests left undecided, and approvals granted but not used, expire after `APPROVAL_TTL_SECONDS`
  (default 24h). Deciding an expired request returns `409` and fails the task; worker recovery also
  fails expired tasks closed.
- A resumed task re-runs from the start, so a task may pause for approval at most
  `MAX_APPROVAL_ROUNDS` times (default 3); the next approval request fails it.

### Explicitly Unsupported Features

To prevent architectural drift and security vulnerabilities, the following are explicitly **not supported**:
- **Live Trading Execution**: Baby is permanently analysis-only. No live broker integrations, API order keys, or automated order placements are permitted.
- **External Brokers / Distributed Queues**: RabbitMQ, Redis, Celery, and Kafka are not supported; SQLite WAL is the sole coordination channel.
- **Unrestricted Shell Execution**: Specialist agents execute strictly within repository-bounded filesystems; direct shell access is blocked.

---

## Development

### Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install dependencies
pip install -e .[dev]
```

### Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=baby tests/

# Run specific test file
pytest tests/test_contracts.py

# Run a single test
pytest tests/test_contracts.py::TestTaskId::test_task_id_creation
```

### Running the observability API and workers

```bash
uvicorn baby.api.app:app --reload
python -m baby.workers run
```

### Code Quality

```bash
# Format code
black baby tests

# Lint code
ruff check baby tests

# Type checking
mypy baby

# Sort imports
isort baby tests
```

### Typed API Contract & TypeScript Client (Phase 13)

Baby exposes a versioned, machine-consumable API contract (`/api/v1/*`) governed by an OpenAPI 3.1.0 specification (`docs/openapi.json`). A standalone, zero-runtime-dependency TypeScript client package (`@baby/client` in `clients/typescript/`) is provided for browser, desktop, and Node.js consumers:

- **Authoritative Backend**: Python FastAPI remains the sole backend, persistence, orchestration, and worker layer.
- **Contract Source of Truth**: `docs/openapi.json` generated directly from FastAPI wire models (`baby/api/schemas.py`).
- **Deterministic Generation**: `python scripts/generate_ts_types.py` generates `clients/typescript/src/types.ts` with zero external npm build dependencies.
- **Strict Client Layer**: Complete typed client (`BabyClient`, `TasksApi`, `WorkersApi`, `ObservabilityApi`) with timeout control, engagement scoping, and structured error hierarchies. Zero Python/database leakage.

```bash
# Regenerate OpenAPI schema and TypeScript definitions
python scripts/generate_ts_types.py

# Verify the committed contract is up to date (CI runs this)
python scripts/generate_ts_types.py --check

# Build and test TypeScript client (Node >= 22.6)
cd clients/typescript
npm ci
npm test
```

### End-to-End Integration & Reliability (Phase 14)

Baby provides deterministic end-to-end integration and failure recovery guarantees across the full execution pipeline:
`TypeScript client → HTTP API (/api/v1) → durable SQLite queue → isolated worker processes → permissions & approvals → SQLite results → audit trail → typed response`.

- **Lifecycle Integration**: End-to-end verification of task submission, background execution by isolated worker processes, result persistence, and audit logging.
- **Fail-Closed Security**: IDOR protection via 404 responses, strict server-side engagement scoping, admin-only approval gating, operation-bound SHA-256 digests, and single-use approval grants with replay prevention.
- **Worker & Lease Resilience**: Fenced task leases prevent late writes by stale workers; API restarts preserve live worker tasks; process crashes trigger fail-closed task recovery ("not replayed"); concurrent workers race safely without duplicate claims.
- **TypeScript Client Integration**: Validated against local mock/ephemeral HTTP services covering full round-trips, timeout control, network error translation, zero unapproved retries on non-idempotent state mutations, and malformed response resilience.

```bash
# Run end-to-end integration tests
pytest tests/test_e2e_integration.py -v

# Run TypeScript client integration tests
cd clients/typescript && npm ci && npm test
```

## Implementation Status

- [x] PHASE 0: Repository inspection
- [x] PHASE 1: Foundation (structure, configuration, contracts, logging, errors, tests)
- [x] PHASE 2: Agent framework
- [x] PHASE 3: Permission system & Tool framework
- [x] PHASE 4: Planner & Orchestration
- [x] PHASE 5: Memory system
- [x] PHASE 6: Model Provider Abstraction & Approval Enforcement
- [x] PHASE 7: Coding agent & repository-bounded tools
- [x] PHASE 8: Iterative coding agent orchestration
- [x] PHASE 9: Operational observability, audit visibility, and API authorization
- [x] PHASE 10: Production reliability and lifecycle hardening
- [x] PHASE 11: Architectural boundaries and durable SQLite persistence
- [x] PHASE 12: Durable worker architecture and process isolation
- [x] PHASE 13: Typed API contract and TypeScript client
- [x] PHASE 14: End-to-end integration and reliability
- [x] PHASE 15: Installation, deployment & first-run experience
- [x] PHASE 16: Security and operational hardening
- [ ] Next: Verification layer, multi-step planning, UI, additional specialist teams

## Security

Baby is designed with security as a core principle:

- **Permission-based access control** - agents have explicit, limited permissions
- **Approval workflows** - high-risk actions require explicit approval
- **Audit logging** - all meaningful actions are recorded
- **Secret management** - API keys and credentials are never committed
- **Least privilege** - agents receive minimal necessary permissions
- **Fail-closed workers** - interrupted work is failed, never replayed; workers cannot grant approvals

## License

MIT
