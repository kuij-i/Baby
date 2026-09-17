# BABY Agent Guide

## What BABY Is

Baby is a personal AI operating system / AI control center built around explicit orchestration, permissions, verification, memory, and audit boundaries.

## Architecture

```text
USER TASK → ORCHESTRATOR → PLANNER → ROUTER/SELECTION → SPECIALIST AGENT
→ SAFE TOOLS → VERIFICATION → MEMORY → AUDIT → RESPONSE
```

## Repository Structure

- `baby/core/` - typed contracts
- `baby/agents/` - agent base class, registry, coding agent
- `baby/planning/` - planner and selection logic
- `baby/orchestration/` - execution flow
- `baby/permissions/` - permissions and approvals
- `baby/tools/` - tool abstractions, executor, safe coding tools
- `baby/verification/` - deterministic verification
- `baby/memory/` - replaceable memory interface and in-memory store
- `baby/audit/` - audit log
- `tests/` - regression tests
- `docs/` - architecture, security, development docs

## Security Principles

- Use explicitly registered tools only
- No unrestricted shell/subprocess access
- No unrestricted filesystem or network access
- Keep write/high-risk actions permission-controlled and approval-gated
- Never log or commit secrets
- Keep verification explicit and auditable

## Current Implementation Status

Implemented:

- core contracts
- agent registry/base class
- planner/selection
- dependency-aware orchestration
- approval-aware tool execution
- deterministic verification
- minimal memory/audit stores
- secure Coding Agent with repository-bounded list/read/write tools

Not implemented yet:

- UI
- durable persistence
- live trading execution
- unrestricted tools
- vector memory / embeddings
- richer multi-agent specialist teams

## Validation Commands

```bash
python -m pytest
python -m ruff check baby tests
python -m black --check baby tests
python -m mypy baby
```

## Development Rules

- Inspect before editing
- Make the smallest correct change
- Add regression tests with each behavior change
- Update documentation when implementation status changes
- Preserve historical phase summaries in `PHASE_*.md`

## More Detail

- `README.md`
- `docs/architecture.md`
- `docs/security.md`
- `docs/development.md`
- `.github/copilot-instructions.md`
