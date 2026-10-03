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
uvicorn baby.api.app:app --reload       # run the read-only observability API
python -m baby.api.openapi              # export OpenAPI schema to docs/openapi.json
```

CI (`.github/workflows/ci.yml`) runs ruff → mypy → pytest on Python 3.10/3.11/3.12 for pushes/PRs to `main`. Markers `unit`, `integration`, `slow` are registered; unregistered markers fail under `--strict-markers`. Prefer `python -m pytest` so tests run with the interpreter that has the package installed.

## Architecture

`PHASE_N_SUMMARY.md` files and commit messages record what each development phase added; `docs/architecture.md` and `docs/security.md` hold the design rationale.

### Execution flow
`orchestration.Orchestrator.execute_task` → `planning.planner.TaskPlanner` (currently produces a single step; `approval_required` when priority ≥ 8) → `planning.selection` picks an enabled agent from `agents.registry` → `Agent.execute(ExecutionContext)` → agent calls tools **only via `tools.executor.ToolExecutor`** → result. Task state is tracked by `observability.tasks.TaskTracker`, and every stage records an `AuditEvent`.

### Security invariants (fail-closed — preserve these)
- `ToolExecutor.execute` validates input, checks each `tool.permissions_required` against `permissions.PermissionManager`, **raises `ApprovalRequiredError` for `APPROVAL_REQUIRED`** (no implicit approval) and `PermissionDeniedError` for denied/missing permissions, audits invocation/result, and enforces `spec.timeout_seconds`. Never execute tools directly, bypassing the executor.
- No shell/subprocess execution anywhere. Coding tools (`tools/coding/`: list/read/write) are confined to `settings.coding_repo_root` via `path_resolver` + `CodingFilesystem`; path traversal raises `PathTraversalError`, any `.git` access raises `RepositoryBoundaryError`. `CodingFilesystem` is the abstract boundary (a native/Rust engine may replace `LocalCodingFilesystem` later) — keep agents and policies coded against the interface.
- CodingAgent: `LOCAL_READ` = ALLOW, `LOCAL_WRITE` = APPROVAL_REQUIRED. Its model loop (`_execute_with_provider`) is bounded by `coding_max_iterations` / `coding_max_tool_calls_per_iteration` and fails the step (not silently skips) on unknown tools, malformed args, or exceeding limits. Without a provider it falls back to a heuristic path.
- Audit details and logs must never contain secrets; `observability/redaction.py` exists for this.

### Core contracts
All cross-component data is Pydantic models in `baby/core/contracts.py` (exported from `baby/core/__init__.py`): `Task`, `Plan`/`PlanStep`, `AgentSpec`, `ToolPermission`, `AgentResult`, `AuditEvent`, `TaskRecord`, `VALID_TASK_TRANSITIONS`, etc. Errors live in `baby/errors.py`.

### Global singletons
Most subsystems are module-level singletons imported directly: `settings`, `audit_log`, `permission_manager`, `agent_registry`, `tool_registry`, `tool_executor`, `provider_registry`, `task_planner`, `selection_logic`, `orchestrator`, `task_tracker`, `worker_tracker`, `metrics_collector`, `health_checker`, `memory_store`, `db_manager`. Tests reset them with autouse fixtures (e.g. `task_tracker.clear()`) and toggle config with `patch("baby.configuration.settings.<field>", ...)` or `patch.object(settings, ...)` — follow the same pattern. Watch for circular imports between `planning` and `orchestration` (previously fixed; `api/app.py` imports lazily inside `lifespan` for this reason).

### Task lifecycle
`TaskTracker` enforces `VALID_TASK_TRANSITIONS` (invalid → `InvalidStateTransitionError`; COMPLETED/FAILED/CANCELLED are terminal). Tasks must be registered PENDING, then moved to IN_PROGRESS. On API startup, `recover_stale_tasks()` marks leftover IN_PROGRESS tasks FAILED.

### Persistence
`persistence/database.DatabaseManager` (SQLite, WAL, transactional) backs `SQLiteTaskStore`, `SQLiteAuditLog`, `SQLiteMemoryStore`; in-memory equivalents implement the same interfaces (`memory.base.MemoryStore`, `audit.AuditLog`). Selected by `persistence_backend` / `memory_storage_type` / `audit_storage_type`. Misconfigured SQLite is a startup `ConfigurationError` — never silently fall back to memory in production.

### Providers
`providers/base.ModelProvider` abstraction + `provider_registry`; `OpenAIProvider` is OpenAI-compatible (`openai_base_url` allows NVIDIA/local endpoints). `is_available()` must return False without credentials.

### Observability API (`baby/api/`)
FastAPI app that is **strictly read-only**: `ReadOnlyMiddleware` returns 405 for non-GET/HEAD/OPTIONS. Every router is mounted twice — at the root and under `/api/v1` — so new routes must be added to both. Auth (`api/auth.py`): bearer tokens (`api_auth_token` = operator, `api_admin_token` = admin). The `X-Engagement-ID` header only *selects* among engagements configured server-side in `operator_engagements`; it must never expand access. `api_require_auth=False` is dev-only (everyone becomes admin). `api/openapi.export_openapi_schema` produces the contract for a future TypeScript client.

## Configuration
`baby/configuration.Settings` (pydantic-settings, reads `.env`, case-insensitive). `validate_settings()` raises on critical misconfig and returns warnings otherwise. Copy `.env.example` to `.env`; secrets (`OPENAI_API_KEY`, API tokens) only come from env.

## Conventions
- Black/ruff/isort at 120 columns; type hints on public functions; Google-style docstrings.
- New contract: add to `core/contracts.py`, export from `core/__init__.py`, test in `tests/test_contracts.py`.
- New tools: subclass `tools.base.Tool`, declare `permissions_required`, register, and test the security constraints (denial/approval paths), not just the happy path.
