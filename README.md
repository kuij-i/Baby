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
  orchestration/         # Main orchestration engine
  planning/              # Task planning subsystem
  permissions/           # Permission and authorization system
  verification/          # Result verification layer
  memory/                # Memory management
  audit/                 # Audit logging
  agents/                # Agent base classes and registry
  tools/                 # Tool abstractions, registry, and executor
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
# Validate formatting, linting, typing, and tests
black --check baby tests
ruff check baby tests
mypy baby
pytest
```

## Implementation Status

### Completed foundation slices

- [x] Phase 1 foundation contracts, configuration, logging, errors, and audit log
- [x] Phase 2 agent base classes and registry
- [x] Phase 3 permission manager, approval workflow storage, tool registry, and safe tool executor
- [x] Phase 4 planning and orchestration skeleton with dependency enforcement
- [x] Phase 5 replaceable in-memory memory store
- [x] Verification boundary for agent results

### Current milestone

- Harden the existing foundation before any Coding Agent implementation
- Keep approvals explicit and auditable
- Keep orchestration bounded and deterministic
- Keep memory simple and replaceable

### Remaining work

- [ ] Implement the Coding Agent milestone
- [ ] Add richer planner/router behavior beyond the current deterministic skeleton
- [ ] Decide how and when orchestrator should persist selective memory records
- [ ] Introduce durable audit/memory storage when a concrete need exists
- [ ] Build UI and additional specialist agents after the foundation proves stable

### Technical debt / deliberate limitations

- The verifier currently performs deterministic result-status checks rather than domain-specific validation
- Planner decomposition remains intentionally simple and does not build a general DAG engine
- Memory is intentionally process-local and opt-in; there is no automatic long-term storage

## Security

Baby is designed with security as a core principle:

- **Permission-based access control** - agents have explicit, limited permissions
- **Approval workflows** - high-risk actions require explicit approval
- **Audit logging** - all meaningful actions are recorded
- **Secret management** - API keys and credentials are never committed
- **Least privilege** - agents receive minimal necessary permissions
- **No auto-approval** - approval-required tool actions stop until an explicit decision is recorded

For detailed security information, see [docs/security.md](docs/security.md).

## Documentation

- [Architecture](docs/architecture.md) - System design and component overview
- [Security](docs/security.md) - Security model and best practices
- [Development](docs/development.md) - Developer guide
- [Copilot Instructions](.github/copilot-instructions.md) - Repository-level guidance

## License

MIT
