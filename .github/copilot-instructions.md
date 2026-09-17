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
  core/                    # Core domain contracts (Phase 1) ✓
  orchestration/           # Orchestration engine
  planning/                # Task planning (Phase 5)
  permissions/             # Permission system and approval storage
  agents/                  # Agent base classes and registry
  tools/                   # Tool abstractions, registry, and executor
  verification/            # Verification boundary for agent results
  memory/                  # Replaceable in-memory memory store
  audit/                   # In-memory audit logging
  logging.py               # Structured logging
  errors.py                # Exceptions

tests/                     # Test suite
docs/                      # Documentation
```

## Implementation Phase

**Completed foundation work**
- [x] Foundation contracts, configuration, logging, errors, and audit log
- [x] Agent base class and registry
- [x] Permission checks, approval request storage, tool registry, and tool executor
- [x] Planner and orchestrator skeleton
- [x] Replaceable in-memory memory store
- [x] Explicit verification boundary for agent results

**Current milestone**
- Harden the existing foundation before any Coding Agent implementation

**Not implemented yet**
- Coding Agent
- UI
- Durable memory/audit storage
- Unrestricted shell, filesystem, or network access

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
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False)
    openai_api_key: str  # Read from OPENAI_API_KEY env var
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

1. Create `baby/agents/my_agent.py`
2. Inherit from base agent class (Phase 2+)
3. Define capabilities in AgentSpec
4. Add tests to `tests/test_agents.py`
5. Register in agent registry (Phase 2+)

### Add a Tool

1. Create tool specification (Phase 4+)
2. Define permissions required
3. Implement tool class
4. Register in tool registry
5. Add tests for security constraints

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
black --check baby tests && ruff check baby tests && mypy baby && pytest

# Commit
git add .
git commit -m "feat: Clear description of change"

# Push and create PR
git push origin feature/description
```

## Next Steps After Phase 1

1. **Phase 2:** Build agent framework
   - Extend beyond the current base class and registry
   - Add concrete specialist agents when the foundation is stable

2. **Phase 3+:** Deepen policy enforcement
   - Domain-specific approval flows
   - Narrower tool constraints
   - Durable audit persistence

3. **Phase 4+:** Improve orchestration
   - Richer planning and routing
   - Selective memory integration
   - Stronger verification strategies

4. **Later milestones**
   - Coding Agent
   - UI
   - Additional specialist teams

## Questions?

Refer to:
- `docs/architecture.md` - How the system works
- `docs/security.md` - Security constraints
- `docs/development.md` - Development process
- `baby/core/contracts.py` - Domain types
