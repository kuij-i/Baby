# Baby

Baby is a personal AI operating system / AI control center built around explicit control-plane boundaries:

```text
USER TASK
  → ORCHESTRATOR
  → PLANNER
  → ROUTER / SELECTION
  → SPECIALIST AGENT
  → SAFE TOOLS
  → VERIFICATION
  → MEMORY
  → AUDIT
  → RESPONSE
```

The repository currently implements a narrow, deterministic vertical slice of that architecture, including a secure Coding Agent milestone.

## Current Implementation Status

Implemented in source today:

- Strongly typed core contracts in `baby/core/`
- Agent base class and registry in `baby/agents/`
- Task planning and selection in `baby/planning/`
- Orchestration with dependency checks and `previous_results` propagation in `baby/orchestration/`
- Permission checks plus explicit approval tracking in `baby/permissions/`
- Registered tool abstractions, registry, and executor in `baby/tools/`
- Deterministic verification in `baby/verification/`
- Minimal replaceable in-memory memory store in `baby/memory/`
- In-memory audit log in `baby/audit/`
- Secure Coding Agent plus repository-bounded coding tools

Historical phase summaries are preserved in `PHASE_*.md` and should be treated as historical context rather than the current source of truth.

## Secure Coding Agent Milestone

This milestone adds a production-oriented Coding Agent that stays inside the hardened control plane.

### Implemented behavior

- Coding tasks can be marked with `Task.metadata["domain"] = "coding"`
- The planner maps supported coding operations to explicit safe tools
- Selection prefers the registered Coding Agent for coding steps
- The Coding Agent only executes allow-listed safe tools
- Repository tools are bounded to a configured repository root
- Write actions require explicit approval before execution
- Results are deterministically verified before the orchestrator accepts them
- Successful verified coding actions store only minimal safe memory summaries
- Important task, approval, tool, and verification events are auditable

### Supported safe coding tools

- `repository_list_files`
- `repository_read_file`
- `repository_write_file`

These tools do **not** expose unrestricted shell access, arbitrary subprocess execution, unrestricted filesystem access, unrestricted network access, or credential handling.

## Repository Structure

```text
baby/
  agents/          # Agent base class, registry, coding agent
  audit/           # Audit log
  core/            # Typed contracts
  memory/          # Replaceable memory interface + in-memory store
  orchestration/   # Task execution flow
  permissions/     # Permission checks + approval tracking
  planning/        # Planner + selection logic
  tools/           # Tool abstractions, executor, safe coding tools
  verification/    # Deterministic verification
  configuration.py
  errors.py
  logging.py

tests/             # Regression tests
docs/              # Architecture, security, development docs
```

## Security Boundaries

- No unrestricted autonomous execution
- No unrestricted shell or arbitrary subprocess execution
- No unrestricted filesystem or network access
- Explicit registered tools only
- Explicit permission checks before tool use
- Approval requests recorded and enforced for high-risk actions
- Deterministic verification boundary before result acceptance
- Audit logging for meaningful execution events
- No secrets in source control

## Development

Install dependencies:

```bash
python -m pip install -e .[dev]
```

Run validation:

```bash
python -m pytest
python -m ruff check baby tests
python -m black --check baby tests
python -m mypy baby
```

## Documentation

- `AGENTS.md` - concise contributor/agent guide
- `docs/architecture.md` - current architecture and coding-agent slice
- `docs/security.md` - security and approval boundaries
- `docs/development.md` - local workflow and validation commands
- `.github/copilot-instructions.md` - repository-specific coding guidance

## Current Technical Debt / Next Milestones

Not implemented yet:

- LLM-backed planning or code generation
- Rich approval persistence/backends
- Durable audit or memory storage
- Additional specialist agents
- UI
- Live trading execution
- Vector memory / embeddings
- Unrestricted execution tools

Recommended next milestone: expand the coding workflow with more safe repository operations and richer deterministic verification, while keeping the same permission, approval, memory, and audit boundaries.
