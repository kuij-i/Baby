# BABY Architecture

## Overview

Baby is a personal AI operating system designed to coordinate multiple specialist AI agents with strong governance, permission controls, and audit logging. The architecture emphasizes:

- **Modular design** with clear separation of concerns
- **Type safety** through strong contracts
- **Security by default** with permission controls
- **Auditability** of all meaningful actions
- **Extensibility** to support new agent types and tools
- **No unrestricted autonomous execution**

## Core Architecture

### Layered Orchestration

```
USER TASK
    ↓
BABY ORCHESTRATOR (Task acceptance, routing, coordination)
    ↓
PLANNER (Decompose task into steps)
    ↓
ROUTER (Select appropriate specialist agent(s))
    ↓
SPECIALIST AGENT (Execute step with tools)
    ↓
TOOLS (Abstracted tool execution)
    ↓
VERIFICATION (Validate results)
    ↓
MEMORY (Store learnings)
    ↓
AUDIT LOG (Record all actions)
    ↓
FINAL RESPONSE
```

### Core Components

#### 1. Orchestrator

The heart of BABY. Responsibilities:
- Receive and validate tasks
- Invoke the planner
- Coordinate agent execution
- Check permissions before execution
- Handle approvals
- Manage execution context
- Coordinate verification
- Record audit events
- Return results to user

#### 2. Planner

Decomposes tasks into executable steps.
- Analyze task requirements
- Determine step sequence
- Identify dependencies
- Flag approval-required steps
- Produce structured Plan

#### 3. Router

Matches tasks/steps to appropriate agents.
- Analyze step requirements
- Match against agent capabilities
- Check agent availability
- Consider resource constraints
- Return agent selection

#### 4. Specialist Agents

Domain-specific AI agents:
- **Coding Agent** - Software development tasks strictly bounded to an explicitly configured repository root (`CODING_REPO_ROOT`). Operates via `ListFilesTool`, `ReadFileTool`, and `WriteFileTool`. Zero shell, zero subprocess, and `.git` protected. Write operations enforce approval.
- **Trading Agent** - Financial analysis and trading
- **Copywriting Agent** - Content creation and editing
- **Testing Agent** - QA and test automation
- **Cybersecurity Agent** - Security assessment and remediation

Each agent:
- Has explicit capabilities
- Has explicit permissions
- Uses approved tools only
- Produces auditable results
- Reports execution metadata

#### 5. Tool System

Abstraction layer for all agent capabilities.

**Tool Abstraction:**
- Name and description
- Input/output schemas
- Required permissions
- Timeout and retry settings
- Risk level
- Audit metadata

**Not Direct Shell Access:**
- No unrestricted `exec()` or `subprocess.run()`
- Tools defined as specifications
- Tool registry for all available tools
- Permission checking before invocation

#### 6. Permission System

Granular access control.

**Permission Categories:**
- `READ_ONLY` - Read file system
- `LOCAL_READ` - Read local data
- `LOCAL_WRITE` - Write local data
- `EXECUTE_COMMAND` - Execute shell commands
- `NETWORK_ACCESS` - Network communication
- `EXTERNAL_API` - Third-party APIs
- `GITHUB_READ` - GitHub read access
- `GITHUB_WRITE` - GitHub write access
- `FINANCIAL_ACTION` - Trading and financial transactions
- `SECURITY_ACTION` - Security operations
- `DESTRUCTIVE_ACTION` - Delete/destroy operations
- `SECRET_ACCESS` - Access credentials/secrets

**Permission Decisions:**
- `ALLOW` - Granted
- `DENY` - Denied
- `APPROVAL_REQUIRED` - Requires explicit user approval

**Authorization Flow:**
```
Agent wants to use Tool
        ↓
Orchestrator checks: Does agent have permission?
        ↓
Permission level: ALLOW? → Execute
                 DENY? → Reject
                 APPROVAL_REQUIRED? → Send to user for approval
```

#### 7. Verification Layer

Validates agent output before accepting results.

**Verification Strategies:**
- Schema validation
- Test execution (for code)
- Expected output validation
- Range/constraint checking
- Integration tests
- Security checks

#### 8. Memory System

Central knowledge storage.

**Memory Types:**
- **Conversation Context** - Current task session
- **Long-term Preferences** - User preferences, agent configurations
- **Project Memory** - Project-specific context
- **Agent Memory** - Agent-specific learnings
- **Factual Memory** - Shared knowledge base
- **Decision Memory** - Previous decisions and reasoning
- **Task History** - Completed tasks and outcomes
- **Execution History** - Tool execution results

**Storage:**
- Initially SQLite
- Interface-based for later backend replacement

#### 9. Audit System

Complete audit trail of all actions.

**Audit Events:**
- Task created
- Plan created
- Agent selected
- Permission checked
- Tool invoked
- Tool result recorded
- Approval requested
- Approval granted/denied
- Artifact created
- Verification completed
- Task completed/failed
- Errors

**Audit Properties:**
- Immutable log
- Timestamp on every event
- User attribution
- Task/Agent attribution
- Full context
- Never log secrets

## Core Contracts

All components communicate through strongly typed contracts (Pydantic models):

- **TaskId** - Unique task identifier
- **AgentId** - Unique agent identifier
- **Task** - User task definition
- **AgentSpec** - Agent configuration and capabilities
- **AgentCapability** - Agent capability definition
- **Plan** - Structured plan for task execution
- **PlanStep** - Single executable step
- **ExecutionContext** - Context for step execution
- **ToolSpec** - Tool specification
- **ToolPermission** - Tool permission definition
- **AgentResult** - Agent execution result
- **VerificationResult** - Verification outcome
- **Artifact** - Created asset
- **MemoryRecord** - Memory storage
- **ApprovalRequest** - Approval workflow
- **AuditEvent** - Audit trail entry

## Specialist Teams

### Coding Team

Responsibilities:
- Code review
- Architecture design
- Implementation
- Testing
- Documentation
- Debugging
- Performance optimization

Roles:
- Software Architect
- Backend Engineer
- Frontend Engineer
- Database Engineer
- DevOps Engineer
- Debugger
- Code Reviewer
- Documentation Engineer

### Trading Team

Responsibilities:
- Market research
- Technical analysis
- Fundamental analysis
- Strategy development
- Backtesting
- Risk management
- Portfolio management
- Execution
- Trading journal

**Critical:** Analysis is separated from execution. Live trading is disabled by default and requires explicit approval.

### Copywriting Team

Responsibilities:
- Research
- Strategy
- Writing
- Editing
- SEO optimization
- Fact checking
- Brand voice management

### Testing Team

Responsibilities:
- Unit testing
- Integration testing
- End-to-end testing
- Regression testing
- Performance testing
- Security testing
- Test planning
- Test reporting

### Cybersecurity Team

Responsibilities:
- Security assessment
- Vulnerability identification
- Exploitation (authorized only)
- Defensive remediation
- Security reporting

**Critical Security Constraint:** Only operate on authorized targets with explicit scope and approval.

## Execution Flow Example

```
1. USER submits task:
   "Implement OAuth2 authentication for the API"

2. ORCHESTRATOR receives task
   - Creates Task object
   - Records audit: TASK_CREATED
   - Invokes Planner

3. PLANNER decomposes:
   - Step 1: Design auth architecture
   - Step 2: Implement OAuth2 provider
   - Step 3: Add user model
   - Step 4: Write tests
   - Step 5: Code review
   - Step 6: Integration testing
   - Records audit: PLAN_CREATED

4. For each step:
   a. ORCHESTRATOR selects agent
      - Step 1,5 → Code Architect
      - Step 2,3 → Backend Engineer
      - Step 4 → Testing Engineer
      - Records audit: AGENT_SELECTED

   b. ORCHESTRATOR checks permissions
      - Agent needs: GITHUB_READ, LOCAL_WRITE, EXECUTE_COMMAND
      - Checks permission database
      - Records audit: PERMISSION_CHECKED

   c. ORCHESTRATOR routes to agent with execution context

   d. AGENT executes with tools
      - Uses available tools within permissions
      - Records execution metadata
      - Returns AgentResult
      - Records audit: TOOL_INVOKED, TOOL_RESULT

   e. VERIFICATION layer validates
      - Run tests for code
      - Validate schema
      - Check constraints
      - Records audit: VERIFICATION_COMPLETED

   f. MEMORY stores relevant information
      - Decision: Why this architecture
      - Fact: User prefers FastAPI
      - History: Implementation completed

5. After all steps:
   - Collect all results
   - Records audit: TASK_COMPLETED
   - Return to user
```

## Non-Functional Requirements

### Security
- No secrets in code
- All permissions checked
- Audit trail for compliance
- Rate limiting where appropriate
- Timeout protection
- Resource limits per agent

### Reliability
- Graceful error handling
- Retry mechanisms for transient failures
- Clear error messages
- Task resumption capability

### Performance
- Async/await for I/O
- Caching where appropriate
- Resource pooling
- Monitoring and metrics

### Maintainability
- Clean code principles
- Comprehensive tests
- Clear documentation
- Modular design
- No tight coupling

## Design Decisions

### Why Not Microservices?

Currently monolithic. Microservices introduce complexity without current demand. If components need independent scaling or separate deployment, can refactor then.

### Why Not Multi-Agent Autonomous Loops?

All actions have boundaries. Every execution has explicit approval points and human oversight. No silent autonomous actions.

### Why Pydantic for Contracts?

Strong validation, serialization, type hints, and ecosystem integration. Helps catch errors at system boundaries.

### Why SQLite Initially?

Simple, file-based, no infrastructure, sufficient for MVP. Can migrate to PostgreSQL when needed without changing application layer (interface-based).

### Why Structured Logging?

Machine-readable logs, better for parsing and analysis, easier to ship to log aggregation systems later.

## Phase 11 Architecture & Language Boundaries

Phase 11 establishes a targeted hybrid architecture with explicit interfaces:

### 1. Python Ownership (Intelligence & Backend Layer)
Python remains the authoritative language for:
- Agent reasoning loops, orchestration, and planning
- LLM provider integration (OpenAI, Anthropic, local endpoints)
- REST control plane & observability API (FastAPI)
- Security permission manager, approval workflow, and token authentication
- Task state machine and lifecycle tracking (TaskTracker)
- Analysis-only trading research and signal generation

### 2. Persistence Boundary (Durable SQLite)
Durable state is decoupled from domain logic via explicit interfaces:
- `DatabaseManager`: Connection lifecycle, WAL journal mode, busy timeouts, and atomic transaction rollback.
- `MemoryStore`: Abstract interface implemented by both `InMemoryStore` and durable `SQLiteMemoryStore`.
- `AuditLog`: Immutable audit trail backed by `SQLiteAuditLog` with indexed queries, pagination, and engagement isolation.
- `SQLiteTaskStore`: Backing store for `TaskTracker` ensuring tasks, plans, approvals, and outcomes survive restarts while the Python domain layer strictly enforces state machine rules and fail-closed crash recovery.

### 3. Future TypeScript Boundary (Presentation & Client Layer)
- TypeScript is **deferred** — no frontend application code was prematurely introduced.
- The Python API serves as the formal contract boundary via OpenAPI 3.1.0 schema generation (`baby.api.openapi.export_openapi_schema`).
- Endpoints are dual-mounted under both unversioned root paths and `/api/v1/...` to establish a clean convention for future breaking changes while maintaining 100% backwards compatibility.

### 4. Future Rust Boundary (Native Runtime Layer)
- Rust is **deferred** — no production code was migrated because the existing Python implementation meets current performance and security requirements without evidence justifying migration.
- Established the `CodingFilesystem` abstract interface in `baby.tools.coding.filesystem`. The current authoritative implementation is `LocalCodingFilesystem`. A future native Rust engine can be introduced behind this interface with zero modifications to `CodingAgent` or security policies.

### 5. Analysis-Only Trading Guardrail
- Trading remains strictly analysis-only.
- Live trading, broker order execution, exchange credentials, and automated position management are explicitly forbidden and non-existent.

## Future Considerations

- Multi-user support and authentication
- Web/desktop UI
- Advanced memory (embeddings, vector DB)
- More specialist teams
- External model providers
- Distributed execution
- Advanced permission delegation
