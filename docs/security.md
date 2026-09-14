# BABY Security Model

## Overview

Security is a core principle of BABY, not an afterthought. The system is designed with explicit authorization, minimal privilege, and complete auditability.

## Core Security Principles

### 1. No Secrets in Code

**Rule:** API keys and credentials are NEVER committed to the repository.

**Implementation:**
- `.env` file is in `.gitignore`
- Use `.env.example` for configuration template
- Environment variables only at runtime
- Pydantic settings for configuration

**Example:**
```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    openai_api_key: str  # Read from OPENAI_API_KEY env var
    github_token: str    # Read from GITHUB_TOKEN env var

    class Config:
        env_file = ".env"
```

### 2. Permission-Based Access Control

**Rule:** Agents have explicit, minimal permissions. No unrestricted access.

**Permission Categories:**
```python
class PermissionCategory(str, Enum):
    READ_ONLY = "read_only"
    LOCAL_READ = "local_read"
    LOCAL_WRITE = "local_write"
    EXECUTE_COMMAND = "execute_command"
    NETWORK_ACCESS = "network_access"
    EXTERNAL_API = "external_api"
    GITHUB_READ = "github_read"
    GITHUB_WRITE = "github_write"
    FINANCIAL_ACTION = "financial_action"
    SECURITY_ACTION = "security_action"
    DESTRUCTIVE_ACTION = "destructive_action"
    SECRET_ACCESS = "secret_access"
```

**Permission Levels:**
- `ALLOW` - Silently permitted
- `DENY` - Rejected immediately
- `APPROVAL_REQUIRED` - User must approve

**Authorization Flow:**
```python
def can_execute_tool(agent: AgentSpec, tool: ToolSpec) -> AuthorizationResult:
    # 1. Check if agent has required permissions
    agent_permissions = get_agent_permissions(agent.id)
    
    # 2. Check tool requirements
    tool_permissions = tool.permissions_required
    
    # 3. For each required permission:
    for perm in tool_permissions:
        decision = check_permission(agent, perm)
        if decision == DENY:
            raise PermissionDeniedError()
        elif decision == APPROVAL_REQUIRED:
            request_user_approval(agent, tool, perm)
            wait_for_approval()  # Blocks until approved or denied
    
    # 4. All permissions granted
    record_audit_event("PERMISSION_CHECKED", granted=True)
    return AuthorizationResult(authorized=True)
```

### 3. No Unrestricted Shell Execution

**Rule:** Agents cannot execute arbitrary shell commands.

**Violation Example (NOT ALLOWED):**
```python
# WRONG - Never do this
result = subprocess.run(user_input, shell=True)  # Arbitrary code execution!
```

**Correct Pattern (REQUIRED):**
```python
# CORRECT - All tools are defined and registered
class GitTool(Tool):
    name = "git"
    allowed_commands = ["clone", "pull", "push", "status"]
    
    def execute(self, command: str, *args: str) -> ToolResult:
        if command not in self.allowed_commands:
            raise PermissionDeniedError(f"Command {command} not allowed")
        
        # Execute only allowed commands with validated args
        result = subprocess.run(["git", command] + list(args))
        return ToolResult(success=True, output=result.stdout)
```

### 4. High-Risk Actions Require Approval

**Rule:** Dangerous operations cannot execute silently.

**High-Risk Actions:**
- Destructive operations (delete, drop)
- Financial transactions (trading, payments)
- Security operations (vulnerability exploitation)
- Secret access (viewing credentials)
- Writing to critical systems

**Example - Financial Action Approval:**
```python
if tool_name == "execute_trade":
    approval = create_approval_request(
        task_id=task.id,
        agent_id=agent.id,
        action_type="trade_execution",
        reason="Place BTC buy order: 0.5 BTC at $40,000",
        risk_level="high"
    )
    
    result = wait_for_approval(approval)
    if not result.approved:
        raise ApprovalDeniedError("Trade execution rejected by user")
    
    # Only proceed if approved
    return execute_trade_tool(params)
```

### 5. Complete Audit Trail

**Rule:** Every meaningful action is recorded for compliance and debugging.

**Audit Events:**
```python
class AuditEventType(str, Enum):
    TASK_CREATED = "task_created"
    PLAN_CREATED = "plan_created"
    AGENT_SELECTED = "agent_selected"
    PERMISSION_CHECKED = "permission_checked"
    TOOL_INVOKED = "tool_invoked"
    TOOL_RESULT = "tool_result"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    ARTIFACT_CREATED = "artifact_created"
    VERIFICATION_COMPLETED = "verification_completed"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    ERROR = "error"
```

**Audit Record:**
```python
class AuditEvent(BaseModel):
    id: UUID
    event_type: AuditEventType
    task_id: Optional[TaskId]
    agent_id: Optional[AgentId]
    timestamp: datetime
    details: Dict[str, Any]
    user_id: Optional[str]
```

**Never Log:**
- API keys
- Passwords
- Session tokens
- Credit card numbers
- Private keys
- Sensitive user data (SSNs, etc.)

## Specialized Security: Trading

### Non-Negotiable Constraints

1. **Analysis vs. Execution Separation**
   - Analysis tools: Read-only data access
   - Execution tools: Separate, explicit approval required
   - No implicit trading from analysis

2. **Live Trading Disabled by Default**
   ```python
   class TradingConfig:
       enable_live_trading = False  # Only enable explicitly
       max_position_size = 0  # Disabled
       max_daily_loss = 0  # Disabled
   ```

3. **Approval for Every Trade**
   ```python
   def execute_trade(order: TradeOrder) -> TradeResult:
       # Even if already approved, require explicit confirmation
       approval = request_trade_approval(
           order=order,
           reason=f"Execute {order.side} {order.quantity} {order.symbol}"
       )
       
       if not approval.approved:
           raise ApprovalDeniedError("Trade rejected")
       
       # Verify order hasn't changed
       if order != approval.approved_order:
           raise VerificationError("Order changed after approval")
       
       # Execute
       return broker_api.place_order(order)
   ```

4. **Risk Limits**
   - Maximum position size
   - Maximum daily loss
   - Maximum leverage
   - Circuit breakers

## Specialized Security: Cybersecurity

### Non-Negotiable Constraints

1. **Authorized Targets Only**
   ```python
   def scan_vulnerability(target: str) -> ScanResult:
       # Check target is in authorized list
       if not is_authorized_target(target):
           raise PermissionDeniedError(f"Target {target} not authorized")
       
       return vulnerability_scanner.scan(target)
   ```

2. **Controlled Exploit Validation**
   - Proof-of-concept only
   - No destructive actions
   - Requires explicit scope
   - No lateral movement
   - Full logging

3. **Defensive Remediation Only**
   - Fix vulnerabilities
   - Hardening
   - Configuration
   - No offensive tools

## Secrets Management

### Environment Variables

```bash
# .env (NOT COMMITTED)
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-...
GITHUB_TOKEN=ghp_...
ALPHAVANTAGE_API_KEY=...
```

### Configuration

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    openai_api_key: str
    anthropic_api_key: Optional[str] = None
    github_token: str
    
    class Config:
        env_file = ".env"
        case_sensitive = True
```

### Usage

```python
from baby.configuration import settings

# In any module
response = openai.ChatCompletion.create(
    api_key=settings.openai_api_key,
    model="gpt-4",
    messages=[...]
)
```

## Verification Checklist

Before committing:

- [ ] No API keys in code
- [ ] No passwords in code
- [ ] No database credentials in code
- [ ] `.env` is in `.gitignore`
- [ ] Secrets come from environment only
- [ ] All high-risk operations require approval
- [ ] All actions are auditable
- [ ] No secrets logged
- [ ] Permission checks before tool execution
- [ ] Tests validate security constraints

## Testing Security

### Permission Tests

```python
def test_agent_cannot_access_unauthorized_tool():
    agent = AgentSpec(
        id=AgentId(id="agent-1"),
        permissions=[PermissionCategory.READ_ONLY]  # Only read
    )
    
    # Try to use write tool
    with pytest.raises(PermissionDeniedError):
        orchestrator.execute_tool(
            agent=agent,
            tool="delete_file",
            params={"path": "/critical/data.db"}
        )
```

### Approval Tests

```python
def test_financial_action_requires_approval():
    order = TradeOrder(symbol="BTC", quantity=1.0, side="BUY")
    
    # Without approval, should fail
    with pytest.raises(ApprovalRequiredError):
        trading_agent.execute_trade(order, approved=False)
```

### Audit Tests

```python
def test_all_actions_are_audited():
    task = Task(title="Test", description="Test")
    orchestrator.execute_task(task)
    
    events = audit_log.get_events(task_id=task.id)
    assert any(e.event_type == AuditEventType.TASK_CREATED for e in events)
    assert any(e.event_type == AuditEventType.TASK_COMPLETED for e in events)
```

## Compliance

BABY is designed to support compliance requirements:

- **Audit Trail** - Complete record of all actions
- **User Attribution** - Who initiated each action
- **Approval History** - Who approved what and when
- **Non-Repudiation** - Can't deny actions taken
- **Access Control** - Limited permissions
- **Data Protection** - No logging of sensitive data

## Incident Response

If a security incident occurs:

1. Check audit logs for the event
2. Identify the agent and task involved
3. Review the approval chain
4. Disable affected agent if needed
5. Review and update permissions
6. Document in security log

## Future Enhancements

- User authentication and MFA
- API rate limiting
- IP whitelisting
- End-to-end encryption for sensitive data
- Key rotation policies
- Security scanning in CI/CD
- SIEM integration
