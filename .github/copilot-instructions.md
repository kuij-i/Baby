# BABY Repository Instructions

## Source of Truth

The source code is authoritative. Historical phase summaries in `PHASE_*.md` are useful context, but implementation decisions must follow the current repository state.

## What BABY Is

Baby is a personal AI operating system / AI control center with explicit governance:

```text
USER TASK → ORCHESTRATOR → PLANNER → ROUTER/SELECTION → SPECIALIST AGENT
→ SAFE TOOLS → VERIFICATION → MEMORY → AUDIT → RESPONSE
```

## Current Implemented Milestone

The repository currently includes:

- typed contracts
- base agent framework and registry
- planner and selection logic
- orchestration with dependency validation and `previous_results`
- permission checks and explicit approval tracking
- tool registry/executor
- deterministic verification
- minimal in-memory memory and audit stores
- a secure repository-bounded Coding Agent

## Coding-Agent Boundaries

The Coding Agent is intentionally narrow.

- Use only explicitly registered safe coding tools
- Current built-in tools are:
  - `repository_list_files`
  - `repository_read_file`
  - `repository_write_file`
- Keep tool execution bounded to the configured repository root
- Never add unrestricted shell access, arbitrary subprocess execution, unrestricted filesystem access, unrestricted network access, or credential handling
- Write/high-risk actions must remain approval-gated
- Verification must remain explicit and deterministic
- Memory storage must stay minimal and avoid source contents or secrets

## Security Rules

- No secrets or credentials in source control
- No approval bypasses
- No permission bypasses
- No automatic verification success without a real verification step
- No direct destructive or external execution paths outside registered tools
- Audit meaningful actions, especially approvals, tool usage, verification, and failures

## Development Rules

- Make the smallest correct change
- Prefer extending existing abstractions over introducing parallel ones
- Preserve unrelated working-tree changes
- Add focused regression tests for each behavior change
- Update docs when implementation status changes

## Validation Commands

```bash
python -m pytest
python -m ruff check baby tests
python -m black --check baby tests
python -m mypy baby
```

## Key Paths

- `baby/core/contracts.py`
- `baby/agents/`
- `baby/planning/`
- `baby/orchestration/`
- `baby/permissions/`
- `baby/tools/`
- `baby/verification/`
- `baby/memory/`
- `baby/audit/`
- `tests/`
- `docs/`
