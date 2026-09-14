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
- **Coding Agent** - Software development tasks
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

## Future Considerations

- Multi-user support and authentication
- Web/desktop UI
- Advanced memory (embeddings, vector DB)
- More specialist teams
- External model providers
- Distributed execution
- Advanced permission delegation
