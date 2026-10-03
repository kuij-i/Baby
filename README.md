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
  persistence/           # SQLite database manager and durable stores
  observability/         # Task/worker tracking, metrics, health, redaction
  api/                   # Read-only FastAPI observability API (+ /api/v1)
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

### Running the observability API

```bash
uvicorn baby.api.app:app --reload
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
- [ ] Next: Verification layer, multi-step planning, UI, additional specialist teams

## Security

Baby is designed with security as a core principle:

- **Permission-based access control** - agents have explicit, limited permissions
- **Approval workflows** - high-risk actions require explicit approval
- **Audit logging** - all meaningful actions are recorded
- **Secret management** - API keys and credentials are never committed
- **Least privilege** - agents receive minimal necessary permissions

## License

MIT
