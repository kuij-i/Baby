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

# Build and test TypeScript client
cd clients/typescript
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
cd clients/typescript && npm test
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
