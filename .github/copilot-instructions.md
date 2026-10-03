# BABY Repository Instructions

## About BABY

Baby is a personal AI operating system / AI control center designed to coordinate multiple specialist AI agents across different domains (coding, trading, copywriting, testing, cybersecurity) with strong governance, permission controls, and audit logging.

## Core Architectural Pattern

```
USER TASK → ORCHESTRATOR → PLANNER → ROUTER → SPECIALIST AGENT → TOOLS → VERIFICATION → MEMORY → AUDIT LOG → RESPONSE
```

**Key Principle:** No unrestricted autonomous execution. All actions have explicit boundaries, permission checks, and audit trails.

## Repository Structure

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
  persistence/           # SQLite database manager and durable stores
  observability/         # Task/worker tracking, metrics, health, redaction
  api/                   # Read-only FastAPI observability API (+ /api/v1)
  configuration.py       # Settings (pydantic-settings, .env)
  logging.py             # Structured logging
  errors.py              # Exception types

tests/                   # Test suite
docs/                    # Documentation
.github/                 # CI workflow and Copilot instructions
```

## Implementation Phase

**Current:** PHASE 11 (Architectural Boundaries & Durable Persistence) - COMPLETE
- [x] Foundation (Phase 1)
- [x] Agent framework & permissions (Phase 2 & 3)
- [x] Tool framework with fail-closed approval enforcement (Phase 3 & 6)
- [x] Planning & orchestration (Phase 4)
- [x] Memory subsystem (Phase 5)
- [x] Model provider abstraction & OpenAI-compatible provider (Phase 6)
- [x] GitHub Actions CI workflow (Phase 6)
- [x] Repository-bounded coding tools & CodingAgent (Phase 7)
- [x] Bounded iterative CodingAgent orchestration loop (Phase 8)
- [x] Read-only observability API, audit visibility, engagement-scoped auth (Phase 9)
- [x] Task state machine, restart recovery, lifecycle hardening (Phase 10)
- [x] CodingFilesystem boundary, /api/v1 versioning, OpenAPI export, SQLite persistence (Phase 11)

**Next:** Verification layer, LLM-driven multi-step planning, UI, additional specialist teams

## Core Contracts

All components communicate using strongly typed Pydantic models:

- `Task` - User task definition
- `Plan` / `PlanStep` - Task decomposition
- `AgentSpec` - Agent configuration
- `AgentCapability` - Agent capability definition
- `ToolSpec` - Tool specification
- `ToolPermission` - Permission definition
- `AgentResult` - Execution result
- `VerificationResult` - Verification outcome
- `ApprovalRequest` - Approval workflow
- `AuditEvent` - Audit trail entry
- `MemoryRecord` - Memory storage
- `Artifact` - Created asset

See `baby/core/contracts.py` for all types.

## Development Standards

### Code Quality
- **Type hints:** All public functions must have type hints
- **Validation:** Use Pydantic for domain objects
- **Testing:** Every meaningful subsystem needs tests
- **Documentation:** Clear docstrings, especially for public APIs
- **Code style:** Black (120-char line), Ruff, MyPy

### Run Before Committing
```bash
black baby tests && ruff check baby tests && mypy baby && pytest
```

## Security Requirements

### CRITICAL: Never Commit
- API keys
- Database passwords
- Session tokens
- Private keys
- Any secrets

**Pattern:**
```python
# Use environment variables and pydantic-settings
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    openai_api_key: str  # Read from OPENAI_API_KEY env var
    
    class Config:
        env_file = ".env"  # .env is in .gitignore
```

### Permission Model

Before executing any agent action:

1. **Check permissions** - Does agent have required permission?
2. **Enforce approval** - High-risk actions require explicit approval
3. **Record audit** - Log all meaningful actions

**High-Risk Categories:**
- `FINANCIAL_ACTION` - Trading, payments (always approval)
- `DESTRUCTIVE_ACTION` - Delete, drop (always approval)
- `SECURITY_ACTION` - Exploit validation (always approval)
- `SECRET_ACCESS` - Credentials (always approval)

### No Unrestricted Tool Access

**WRONG:**
```python
# Never allow arbitrary execution
result = subprocess.run(user_input, shell=True)
```

**CORRECT:**
```python
# Tools must be explicitly defined and registered
class GitTool(Tool):
    allowed_commands = ["clone", "pull", "push", "status"]
    
    def execute(self, command: str, *args: str) -> ToolResult:
        if command not in self.allowed_commands:
            raise PermissionDeniedError()
        return subprocess.run(["git", command] + list(args))
```

## Testing Approach

### Required Tests

- **Contracts:** Validation, serialization, edge cases
- **Permissions:** Authorization decisions
- **Agent execution:** Success and failure paths
- **Verification:** Output validation
- **Audit logging:** Event recording

### Test Command

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=baby tests/

# Run specific test
pytest tests/test_contracts.py::TestTaskId::test_task_id_creation
```

## Design Principles

### DO:
- Use type safety (Pydantic, type hints)
- Separate concerns (orchestrator, planner, agents, tools)
- Validate at boundaries (input validation)
- Record audit trails (every action)
- Require approval for risky actions
- Use environment variables for secrets
- Test meaningful subsystems
- Document architectural decisions
- Keep interfaces explicit
- Keep modules focused

### DON'T:
- Hard-code API keys or passwords
- Allow unrestricted shell execution
- Skip permission checks
- Silent approval of risky actions
- Over-engineer with microservices
- Create unnecessary abstraction
- Log sensitive data
- Bypass verification layers
- Create dead code
- Add fake implementations

## Common Tasks

### Add a New Domain Contract

1. Add to `baby/core/contracts.py`
2. Export from `baby/core/__init__.py`
3. Add tests to `tests/test_contracts.py`
4. Run `pytest` to verify

### Add an Agent Type

1. Create a package under `baby/agents/` (see `baby/agents/coding/`)
2. Inherit from `baby.agents.base.Agent`
3. Define capabilities and permissions in its `AgentSpec`
4. Add tests to `tests/test_agents.py`
5. Register in `baby.agents.registry.agent_registry`

### Add a Tool

1. Subclass `baby.tools.base.Tool` with a `ToolSpec`
2. Declare `permissions_required`
3. Execute only through `baby.tools.executor.tool_executor`
4. Register in `baby.tools.registry.tool_registry`
5. Add tests for security constraints (denial and approval paths)

## Useful Files

- `README.md` - Project overview
- `docs/architecture.md` - System design
- `docs/security.md` - Security model
- `docs/development.md` - Dev guide
- `baby/core/contracts.py` - All domain types
- `pyproject.toml` - Dependencies and configuration
- `tests/test_contracts.py` - Contract tests (reference)

## Git Workflow

```bash
# Create feature branch
git checkout -b feature/description

# Make changes, add tests

# Verify quality
black baby tests && ruff check baby tests && mypy baby && pytest

# Commit
git add .
git commit -m "feat: Clear description of change"

# Push and create PR
git push origin feature/description
```

## Questions?

Refer to:
- `docs/architecture.md` - How the system works
- `docs/security.md` - Security constraints
- `docs/development.md` - Development process
- `baby/core/contracts.py` - Domain types
