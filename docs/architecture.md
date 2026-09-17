# BABY Architecture

## Overview

Baby is a modular AI control plane with explicit boundaries between planning, execution, verification, memory, and audit.

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

The current repository implements a deterministic vertical slice of this design, including a secure Coding Agent.

## Implemented Components

### Core contracts

`baby/core/contracts.py` defines the typed contracts shared across the system:

- `Task`
- `Plan` / `PlanStep`
- `ExecutionContext`
- `AgentSpec` / `AgentResult`
- `ToolSpec` / `ToolPermission`
- `VerificationResult`
- `ApprovalRequest`
- `MemoryRecord`
- `AuditEvent`

Contracts currently use timezone-aware UTC timestamps.

### Agents

`baby/agents/` contains:

- `Agent` base class
- `AgentRegistry`
- `CodingAgent`

The Coding Agent is intentionally narrow. It executes only allow-listed, repository-bounded coding tools and does not expose arbitrary shell or network capabilities.

### Planning and selection

`baby/planning/` provides:

- `TaskPlanner` for step creation
- `SelectionLogic` for agent/tool selection

Coding tasks are identified through task metadata and routed through the existing planner/selection path rather than bypassing orchestration.

### Orchestration

`baby/orchestration/orchestrator.py` coordinates:

- task audit creation
- plan creation
- dependency validation
- dependency failure handling
- `previous_results` propagation to dependent steps
- agent execution
- deterministic verification
- minimal safe memory storage
- completion/failure audit events

The orchestrator rejects invalid dependency references and circular step dependencies.

### Permissions and approvals

`baby/permissions/` contains:

- `PermissionManager`
- `ApprovalManager`

Tool execution has three real outcomes:

- `ALLOW`
- `DENY`
- `APPROVAL_REQUIRED`

Approval-required actions stop until an explicit approval decision is recorded.

### Tools

`baby/tools/` contains:

- `Tool` base class
- `ToolRegistry`
- `ToolExecutor`
- repository-bounded coding tools

Current built-in safe coding tools:

- `repository_list_files`
- `repository_read_file`
- `repository_write_file`

These tools validate inputs, enforce permission checks, record audit events, and stay inside a configured repository root.

### Verification

`baby/verification/` contains deterministic verification that inspects `AgentResult` objects and produces typed `VerificationResult` records.

The current verifier checks:

- execution success/failure consistency
- step/result alignment
- coding-tool allow-list usage
- coding-tool output shape
- basic path existence for verified repository operations

### Memory

`baby/memory/` provides a replaceable `MemoryStore` interface and an `InMemoryStore` implementation.

Current orchestration stores only minimal safe summaries for verified coding actions:

- tool name
- repository paths
- dependency references used

Source contents and secrets are not automatically persisted.

### Audit

`baby/audit/` provides an in-memory audit log used across orchestration, approvals, and tool execution.

Important events include:

- task created
- plan created
- agent selected
- permission checked
- approval requested / granted / denied
- tool invoked / tool result
- verification completed
- task completed / failed
- error

## Current Limitations

Deliberately not implemented in this milestone:

- LLM-backed planning or code synthesis
- unrestricted execution tools
- durable memory or audit persistence
- UI-mediated approval flows
- vector memory / embeddings
- live trading execution
- additional specialist-agent teams

## Recommended Next Milestone

Expand the coding vertical slice with additional safe repository operations and richer deterministic verification while keeping the same planner, approval, memory, and audit boundaries.
