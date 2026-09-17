# BABY Agent Guide

## Purpose

BABY is a personal AI operating system / orchestration platform for bounded,
auditable specialist-agent workflows. The current repository is hardening the
shared foundation before any Coding Agent implementation.

## Repository Structure

```text
baby/
  core/            contracts and domain types
  agents/          agent base class and registry
  permissions/     permission checks and approval request storage
  tools/           tool abstraction, registry, executor
  planning/        task planning and selection logic
  orchestration/   orchestrator with dependency and verification boundaries
  verification/    verifier abstraction and default verifier
  memory/          replaceable in-memory memory store
  audit/           in-memory audit log
tests/             regression and subsystem tests
docs/              architecture, security, development docs
```

## Architecture

`Task -> Plan -> Orchestrator -> Agent -> ToolExecutor -> Verification -> Audit`

- No unrestricted autonomous execution
- No unrestricted shell/filesystem/network access
- High-risk tool actions require explicit approval
- Verification is an explicit boundary, not an assumption
- Memory remains simple, process-local, and replaceable

## Security Principles

- Never commit secrets
- Enforce least privilege through `PermissionManager`
- Never auto-approve `APPROVAL_REQUIRED`
- Audit permission checks, approval decisions, tool usage, and verification
- Keep trading approval-gated and cybersecurity limited to authorized targets

## Development / Validation

Run from the repository root:

```bash
python -m pip install -e ".[dev]"
pytest
ruff check baby tests
black --check baby tests
mypy baby
```

## Constraints

- Make the smallest correct changes
- Do not implement the Coding Agent, UI, vector DB, live trading execution, or unrestricted access layers here
- Keep memory integration selective and auditable
- Update docs when implementation reality changes

## Deeper Docs

- `README.md`
- `docs/architecture.md`
- `docs/security.md`
- `docs/development.md`
- `.github/copilot-instructions.md`

## Current Status

- Implemented: foundation contracts, registries, permission/approval flow, tool executor, planner/orchestrator, verifier, in-memory memory, audit log
- Remaining: concrete specialist agents, richer planning/routing, selective memory persistence, durable storage, UI
