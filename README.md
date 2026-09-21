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
  core/                  # Core domain contracts and types
  orchestrator/          # Main orchestration engine
  planning/              # Task planning subsystem
  routing/               # Agent routing logic
  permissions/           # Permission and authorization system
  verification/          # Result verification layer
  memory/                # Memory management
  audit/                 # Audit logging
  agents/                # Specialist agent implementations
    coding/
    trading/
    copywriting/
    testing/
    cybersecurity/
  tools/                 # Tool implementations and abstractions
  configuration.py       # Configuration management
  logging.py             # Structured logging
  errors.py              # Exception types

tests/                   # Test suite
docs/                    # Documentation
.github/                 # GitHub workflows and instructions
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

# Run unit tests only
pytest -m unit
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
- [x] PHASE 1: Foundation
  - [x] Project structure
  - [x] Configuration
  - [x] Typed contracts
  - [x] Logging infrastructure
  - [x] Error handling
  - [x] Testing foundation
- [x] PHASE 2: Agent framework
- [x] PHASE 3: Permission system & Tool framework
- [x] PHASE 4: Planner & Orchestration
- [x] PHASE 5: Memory system
- [x] PHASE 6: Model Provider Abstraction & Approval Enforcement
- [x] PHASE 7: Coding agent & repository-bounded tools
- [x] PHASE 8: Iterative coding agent orchestration
- [ ] PHASE 9: First complete vertical slice
- [ ] PHASE 10: UI
- [ ] PHASE 11: Additional specialist teams

## Security

Baby is designed with security as a core principle:

- **Permission-based access control** - agents have explicit, limited permissions
- **Approval workflows** - high-risk actions require explicit approval
- **Audit logging** - all meaningful actions are recorded
- **Secret management** - API keys and credentials are never committed
- **Least privilege** - agents receive minimal necessary permissions

For detailed security information, see [docs/security.md](docs/security.md).

## Documentation

- [Architecture](docs/architecture.md) - System design and component overview
- [Security](docs/security.md) - Security model and best practices
- [Development](docs/development.md) - Developer guide
- [Copilot Instructions](.github/copilot-instructions.md) - Repository-level guidance

## License

MIT
