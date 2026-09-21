"""Tests for CodingAgent and security regression guarantees."""

import inspect
import tempfile
from pathlib import Path
from typing import List, Optional

import pytest

from baby.agents.coding.agent import (
    CodingAgent,
    create_default_coding_spec,
)
from baby.agents.registry import agent_registry
from baby.audit import audit_log
from baby.core import (
    AuditEventType,
    ExecutionContext,
    PermissionCategory,
    PermissionLevel,
    PlanStep,
    Task,
    ToolPermission,
)
from baby.permissions import permission_manager
from baby.providers.base import ModelProvider
from baby.providers.types import ProviderResponse, TokenUsage


@pytest.fixture
def temp_repo():
    """Create a temporary repository directory for agent tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir).resolve()
        (repo_path / "main.py").write_text("print('baby')", encoding="utf-8")
        yield repo_path


class MockProvider(ModelProvider):
    """Mock model provider for testing CodingAgent."""

    def __init__(
        self,
        response_content: str = "Mock response",
        tool_calls=None,
        responses: Optional[List[ProviderResponse]] = None,
        repeat_tool_calls: bool = False,
        raise_on_iteration: Optional[int] = None,
    ) -> None:
        self._content = response_content
        self._tool_calls = tool_calls
        self._responses = list(responses) if responses is not None else None
        self._repeat_tool_calls = repeat_tool_calls
        self._raise_on_iteration = raise_on_iteration
        self.call_count = 0
        self.recorded_messages = []
        self.call_history = []

    @property
    def name(self) -> str:
        return "mock"

    @property
    def supported_models(self):
        return ["gpt-4"]

    def is_available(self) -> bool:
        return True

    async def complete(self, messages, **kwargs):
        self.recorded_messages = list(messages)
        return ProviderResponse(content=self._content, model="gpt-4")

    async def complete_with_tools(self, messages, tools, **kwargs):
        self.call_count += 1
        self.recorded_messages = list(messages)
        self.call_history.append(list(messages))

        if self._raise_on_iteration and self.call_count == self._raise_on_iteration:
            raise RuntimeError(f"Simulated provider failure at iteration {self.call_count}")

        if self._responses is not None:
            if self._responses:
                return self._responses.pop(0)
            return ProviderResponse(
                content=self._content,
                model="gpt-4",
                tool_calls=None,
                usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            )

        # Single tool_calls specified: return on call 1 (or repeat if requested), then finish
        if self._tool_calls and (self.call_count == 1 or self._repeat_tool_calls):
            return ProviderResponse(
                content=self._content,
                model="gpt-4",
                tool_calls=self._tool_calls,
                usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            )

        return ProviderResponse(
            content=self._content,
            model="gpt-4",
            tool_calls=None,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        )


class TestCodingAgentBasics:
    """Tests for agent initialization, spec, and tool definitions."""

    def test_default_spec(self) -> None:
        spec = create_default_coding_spec("coding-agent")
        assert spec.id.id == "coding-agent"
        assert spec.name == "CodingAgent"
        assert spec.role == "coding"
        assert len(spec.capabilities) == 2
        caps = {c.name for c in spec.capabilities}
        assert "read_code" in caps
        assert "write_code" in caps

    def test_agent_tools_exposure(self, temp_repo: Path) -> None:
        agent = CodingAgent(repo_root=str(temp_repo))
        assert "list_files" in agent.tools
        assert "read_file" in agent.tools
        assert "write_file" in agent.tools

        tool_defs = agent.get_tool_definitions()
        assert len(tool_defs) == 3
        tool_names = {t.name for t in tool_defs}
        assert tool_names == {"list_files", "read_file", "write_file"}

    def test_agent_registry_registration(self, temp_repo: Path) -> None:
        agent_registry.clear()
        agent = CodingAgent(repo_root=str(temp_repo))
        agent_registry.register(agent)

        retrieved = agent_registry.get("coding-agent")
        assert retrieved.name == "CodingAgent"
        assert retrieved.role == "coding"

        coding_agents = agent_registry.get_agents_by_role("coding")
        assert len(coding_agents) == 1


class TestCodingAgentExecution:
    """Tests for CodingAgent step execution."""

    def setup_method(self) -> None:
        permission_manager.clear()
        audit_log.clear()

    @pytest.mark.asyncio
    async def test_execute_direct_read_tool_step(self, temp_repo: Path) -> None:
        agent = CodingAgent(repo_root=str(temp_repo))

        # Grant read permission
        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_READ,
                level=PermissionLevel.ALLOW,
                description="Read",
            ),
        )

        task = Task(title="Read main", description="Read main.py")
        step = PlanStep(
            step_id=1,
            description='{"path": "main.py"}',
            tool_name="read_file",
        )
        context = ExecutionContext(
            task_id=task.id,
            plan_step=step,
            task=task,
            agent=agent.spec,
        )

        result = await agent.execute(context)
        assert result.success is True
        assert result.output["content"] == "print('baby')"

    @pytest.mark.asyncio
    async def test_execute_write_blocked_when_approval_required(self, temp_repo: Path) -> None:
        agent = CodingAgent(repo_root=str(temp_repo))

        # Grant write permission as APPROVAL_REQUIRED
        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_WRITE,
                level=PermissionLevel.APPROVAL_REQUIRED,
                description="Write needs approval",
            ),
        )

        task = Task(title="Write code", description="Write code.py")
        step = PlanStep(
            step_id=1,
            description='{"path": "code.py", "content": "x = 10"}',
            tool_name="write_file",
        )
        context = ExecutionContext(
            task_id=task.id,
            plan_step=step,
            task=task,
            agent=agent.spec,
        )

        result = await agent.execute(context)
        assert result.success is False
        assert "requires approval" in result.error
        # Target file must NOT exist
        assert not (temp_repo / "code.py").exists()

    @pytest.mark.asyncio
    async def test_execute_with_model_provider_tool_calls(self, temp_repo: Path) -> None:
        """When provider returns a tool call, agent executes tool via tool_executor."""
        tool_call = {
            "id": "tc_1",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": '{"path": "main.py"}',
            },
        }
        provider = MockProvider(response_content="File analyzed", tool_calls=[tool_call])
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo))

        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_READ,
                level=PermissionLevel.ALLOW,
                description="Read",
            ),
        )

        task = Task(title="Analyze", description="Analyze main.py")
        step = PlanStep(step_id=1, description="Inspect repository code")
        context = ExecutionContext(
            task_id=task.id,
            plan_step=step,
            task=task,
            agent=agent.spec,
        )

        result = await agent.execute(context)
        assert result.success is True
        assert result.tokens_used == 40
        assert len(result.output["tool_results"]) == 1
        assert result.output["tool_results"][0]["tool"] == "read_file"
        assert result.output["tool_results"][0]["result"]["content"] == "print('baby')"

    @pytest.mark.asyncio
    async def test_execute_provider_list_files(self, temp_repo: Path) -> None:
        tool_call = {
            "id": "tc_list",
            "type": "function",
            "function": {
                "name": "list_files",
                "arguments": '{"path": "."}',
            },
        }
        provider = MockProvider(response_content="Files listed", tool_calls=[tool_call])
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo))
        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_READ,
                level=PermissionLevel.ALLOW,
                description="Read",
            ),
        )
        task = Task(title="List", description="List repo")
        step = PlanStep(step_id=1, description="List files")
        context = ExecutionContext(task_id=task.id, plan_step=step, task=task, agent=agent.spec)

        result = await agent.execute(context)
        assert result.success is True
        assert len(result.output["tool_results"]) == 1
        assert result.output["tool_results"][0]["tool"] == "list_files"

    @pytest.mark.asyncio
    async def test_execute_provider_write_file_approval_required(self, temp_repo: Path) -> None:
        tool_call = {
            "id": "tc_write",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": '{"path": "from_model.py", "content": "x = 1"}',
            },
        }
        provider = MockProvider(response_content="Writing code", tool_calls=[tool_call])
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo))
        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_WRITE,
                level=PermissionLevel.APPROVAL_REQUIRED,
                description="Write needs approval",
            ),
        )
        task = Task(title="Write", description="Write file")
        step = PlanStep(step_id=1, description="Write from model")
        context = ExecutionContext(task_id=task.id, plan_step=step, task=task, agent=agent.spec)

        result = await agent.execute(context)
        assert result.success is False
        assert "requires approval" in result.error
        assert not (temp_repo / "from_model.py").exists()

    @pytest.mark.asyncio
    async def test_execute_provider_write_file_allowed(self, temp_repo: Path) -> None:
        tool_call = {
            "id": "tc_write_allow",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": '{"path": "allowed_model.py", "content": "x = 99"}',
            },
        }
        provider = MockProvider(response_content="Writing code", tool_calls=[tool_call])
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo))
        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_WRITE,
                level=PermissionLevel.ALLOW,
                description="Write allowed",
            ),
        )
        task = Task(title="Write", description="Write file")
        step = PlanStep(step_id=1, description="Write allowed")
        context = ExecutionContext(task_id=task.id, plan_step=step, task=task, agent=agent.spec)

        result = await agent.execute(context)
        assert result.success is True
        assert (temp_repo / "allowed_model.py").read_text(encoding="utf-8") == "x = 99"

    @pytest.mark.asyncio
    async def test_execute_provider_unknown_tool_fails_safely(self, temp_repo: Path) -> None:
        tool_call = {
            "id": "tc_unknown",
            "type": "function",
            "function": {
                "name": "execute_shell_command",
                "arguments": '{"command": "whoami"}',
            },
        }
        provider = MockProvider(response_content="Executing command", tool_calls=[tool_call])
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo))
        task = Task(title="Unknown", description="Unknown tool call")
        step = PlanStep(step_id=1, description="Run command")
        context = ExecutionContext(task_id=task.id, plan_step=step, task=task, agent=agent.spec)

        result = await agent.execute(context)
        assert result.success is False
        assert "Unknown or missing tool" in result.error

    @pytest.mark.asyncio
    async def test_execute_provider_malformed_arguments_fails_safely(self, temp_repo: Path) -> None:
        tool_call = {
            "id": "tc_bad_args",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": "not valid json {",
            },
        }
        provider = MockProvider(response_content="Reading", tool_calls=[tool_call])
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo))
        task = Task(title="Malformed", description="Bad args tool call")
        step = PlanStep(step_id=1, description="Read with bad args")
        context = ExecutionContext(task_id=task.id, plan_step=step, task=task, agent=agent.spec)

        result = await agent.execute(context)
        assert result.success is False
        assert "Malformed arguments" in result.error


# ---------------------------------------------------------------------------
# Phase 8: Bounded Iterative Loop Tests
# ---------------------------------------------------------------------------


class TestCodingAgentIterativeLoop:
    """Tests for Phase 8 bounded iterative execution loop."""

    def setup_method(self) -> None:
        permission_manager.clear()
        audit_log.clear()

    @pytest.mark.asyncio
    async def test_iterative_multi_step_sequence(self, temp_repo: Path) -> None:
        """Verify model can execute a multi-step sequence: list -> read -> write -> finish."""
        responses = [
            # Iteration 1: list files
            ProviderResponse(
                content="Checking directory contents",
                model="gpt-4",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": '{"path": "."}'},
                    }
                ],
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            ),
            # Iteration 2: read main.py
            ProviderResponse(
                content="Inspecting main.py",
                model="gpt-4",
                tool_calls=[
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "main.py"}'},
                    }
                ],
                usage=TokenUsage(prompt_tokens=15, completion_tokens=5, total_tokens=20),
            ),
            # Iteration 3: write helper.py
            ProviderResponse(
                content="Writing helper module",
                model="gpt-4",
                tool_calls=[
                    {
                        "id": "call_3",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path": "helper.py", "content": "def helper(): return True"}',
                        },
                    }
                ],
                usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
            ),
            # Iteration 4: final answer without tool calls
            ProviderResponse(
                content="Successfully inspected code and created helper.py",
                model="gpt-4",
                tool_calls=None,
                usage=TokenUsage(prompt_tokens=25, completion_tokens=15, total_tokens=40),
            ),
        ]

        provider = MockProvider(responses=responses)
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo))

        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_READ,
                level=PermissionLevel.ALLOW,
                description="Read",
            ),
        )
        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_WRITE,
                level=PermissionLevel.ALLOW,
                description="Write",
            ),
        )

        task = Task(title="Add helper", description="Inspect and add helper.py")
        step = PlanStep(step_id=1, description="Create helper module based on main.py")
        context = ExecutionContext(task_id=task.id, plan_step=step, task=task, agent=agent.spec)

        result = await agent.execute(context)
        assert result.success is True
        assert result.output["iterations"] == 4
        assert len(result.output["tool_results"]) == 3
        assert result.output["response"] == "Successfully inspected code and created helper.py"
        assert result.tokens_used == 105

        # Check created file
        assert (temp_repo / "helper.py").read_text(encoding="utf-8") == "def helper(): return True"

        # Verify tool result feedback across call history
        assert len(provider.call_history) == 4
        # Call 2 should receive list_files result
        call_2_msgs = provider.call_history[1]
        assert any(m.role == "tool" and m.name == "list_files" for m in call_2_msgs)
        # Call 3 should receive read_file result
        call_3_msgs = provider.call_history[2]
        assert any(m.role == "tool" and m.name == "read_file" for m in call_3_msgs)
        # Call 4 should receive write_file result
        call_4_msgs = provider.call_history[3]
        assert any(m.role == "tool" and m.name == "write_file" for m in call_4_msgs)

    @pytest.mark.asyncio
    async def test_iterative_write_approval_stops_loop(self, temp_repo: Path) -> None:
        """When a multi-step sequence hits an unapproved write_file, execution stops immediately."""
        responses = [
            ProviderResponse(
                content="Listing files",
                model="gpt-4",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": '{"path": "."}'},
                    }
                ],
            ),
            ProviderResponse(
                content="Writing file without approval",
                model="gpt-4",
                tool_calls=[
                    {
                        "id": "call_2",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path": "unapproved.py", "content": "x = 1"}',
                        },
                    }
                ],
            ),
            ProviderResponse(
                content="This should never be reached",
                model="gpt-4",
                tool_calls=None,
            ),
        ]

        provider = MockProvider(responses=responses)
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo))

        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_READ,
                level=PermissionLevel.ALLOW,
                description="Read",
            ),
        )
        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_WRITE,
                level=PermissionLevel.APPROVAL_REQUIRED,
                description="Write requires approval",
            ),
        )

        task = Task(title="Modify", description="Modify code")
        step = PlanStep(step_id=1, description="Attempt unapproved write in loop")
        context = ExecutionContext(task_id=task.id, plan_step=step, task=task, agent=agent.spec)

        result = await agent.execute(context)
        assert result.success is False
        assert "requires approval" in result.error
        assert not (temp_repo / "unapproved.py").exists()
        assert provider.call_count == 2  # Third iteration never reached

    @pytest.mark.asyncio
    async def test_iterative_iteration_limit_exhausted(self, temp_repo: Path) -> None:
        """Loop terminates safely when max_iterations is reached without infinite recursion."""
        tool_call = {
            "id": "call_loop",
            "type": "function",
            "function": {"name": "list_files", "arguments": '{"path": "."}'},
        }
        provider = MockProvider(response_content="Keep listing", tool_calls=[tool_call], repeat_tool_calls=True)
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo), max_iterations=3)

        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_READ,
                level=PermissionLevel.ALLOW,
                description="Read",
            ),
        )

        task = Task(title="Loop test", description="Loop exhaustion")
        step = PlanStep(step_id=1, description="Infinite tool calls")
        context = ExecutionContext(task_id=task.id, plan_step=step, task=task, agent=agent.spec)

        result = await agent.execute(context)
        assert result.success is False
        assert "maximum iteration limit of 3" in result.error
        assert result.output["iterations"] == 3
        assert len(result.output["tool_results"]) == 3
        assert provider.call_count == 3

        # Verify audit event for iteration exhaustion
        error_events = audit_log.get_events(event_type=AuditEventType.ERROR)
        assert len(error_events) >= 1
        assert error_events[-1].details.get("error_type") == "iteration_limit_exceeded"

    @pytest.mark.asyncio
    async def test_iterative_tool_call_limit_per_iteration(self, temp_repo: Path) -> None:
        """Exceeding max tool calls in a single response halts execution safely."""
        tool_calls = [
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": "list_files", "arguments": '{"path": "."}'},
            }
            for i in range(6)
        ]
        provider = MockProvider(response_content="Too many tools", tool_calls=tool_calls)
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo), max_tool_calls_per_iteration=5)

        task = Task(title="Explosion test", description="Tool call explosion")
        step = PlanStep(step_id=1, description="Too many tool calls")
        context = ExecutionContext(task_id=task.id, plan_step=step, task=task, agent=agent.spec)

        result = await agent.execute(context)
        assert result.success is False
        assert "exceeding per-iteration limit of 5" in result.error
        assert result.output["iterations"] == 1
        assert len(result.output["tool_results"]) == 0

    @pytest.mark.asyncio
    async def test_iterative_provider_failure_preserves_outputs(self, temp_repo: Path) -> None:
        """Provider failure on later iteration terminates gracefully while preserving prior outputs."""
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "list_files", "arguments": '{"path": "."}'},
        }
        provider = MockProvider(
            response_content="List then fail",
            tool_calls=[tool_call],
            raise_on_iteration=2,
        )
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo))

        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_READ,
                level=PermissionLevel.ALLOW,
                description="Read",
            ),
        )

        task = Task(title="Provider fail", description="Fail on second call")
        step = PlanStep(step_id=1, description="List then provider crash")
        context = ExecutionContext(task_id=task.id, plan_step=step, task=task, agent=agent.spec)

        result = await agent.execute(context)
        assert result.success is False
        assert "Model provider error" in result.error
        assert len(result.output["tool_results"]) == 1

    @pytest.mark.asyncio
    async def test_iterative_traversal_blocked_in_loop(self, temp_repo: Path) -> None:
        """Traversal attempt inside iterative loop fails closed and halts execution."""
        tool_call = {
            "id": "call_escape",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "../../outside_secret.txt"}'},
        }
        provider = MockProvider(response_content="Reading secret", tool_calls=[tool_call])
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo))

        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_READ,
                level=PermissionLevel.ALLOW,
                description="Read",
            ),
        )

        task = Task(title="Escape", description="Escape test")
        step = PlanStep(step_id=1, description="Read outside root")
        context = ExecutionContext(task_id=task.id, plan_step=step, task=task, agent=agent.spec)

        result = await agent.execute(context)
        assert result.success is False
        assert "traverses outside" in result.error

    @pytest.mark.asyncio
    async def test_iterative_git_blocked_in_loop(self, temp_repo: Path) -> None:
        """Access to .git inside iterative loop fails closed and halts execution."""
        tool_call = {
            "id": "call_git",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": ".git/config"}'},
        }
        provider = MockProvider(response_content="Reading git", tool_calls=[tool_call])
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo))

        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_READ,
                level=PermissionLevel.ALLOW,
                description="Read",
            ),
        )

        task = Task(title="Git block", description="Git test")
        step = PlanStep(step_id=1, description="Read git config")
        context = ExecutionContext(task_id=task.id, plan_step=step, task=task, agent=agent.spec)

        result = await agent.execute(context)
        assert result.success is False
        assert "forbidden" in result.error

    @pytest.mark.asyncio
    async def test_iterative_audit_events_recorded(self, temp_repo: Path) -> None:
        """Iterative tool executions generate full audit events."""
        responses = [
            ProviderResponse(
                content="Listing",
                model="gpt-4",
                tool_calls=[
                    {"id": "c1", "type": "function", "function": {"name": "list_files", "arguments": '{"path": "."}'}}
                ],
            ),
            ProviderResponse(
                content="Reading",
                model="gpt-4",
                tool_calls=[
                    {
                        "id": "c2",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "main.py"}'},
                    }
                ],
            ),
            ProviderResponse(content="Done", model="gpt-4", tool_calls=None),
        ]
        provider = MockProvider(responses=responses)
        agent = CodingAgent(provider=provider, repo_root=str(temp_repo))

        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(category=PermissionCategory.LOCAL_READ, level=PermissionLevel.ALLOW, description="Read"),
        )

        task = Task(title="Audit", description="Audit test")
        step = PlanStep(step_id=1, description="Audit check")
        context = ExecutionContext(task_id=task.id, plan_step=step, task=task, agent=agent.spec)

        result = await agent.execute(context)
        assert result.success is True

        invoked = audit_log.get_events(event_type=AuditEventType.TOOL_INVOKED)
        results = audit_log.get_events(event_type=AuditEventType.TOOL_RESULT)
        assert len(invoked) == 2
        assert len(results) == 2
        assert invoked[0].details["tool"] == "list_files"
        assert invoked[1].details["tool"] == "read_file"
        assert results[0].details["success"] is True
        assert results[1].details["success"] is True


# ---------------------------------------------------------------------------
# Security Invariant & Regression Checks
# ---------------------------------------------------------------------------


class TestCodingSubsystemSecurityInvariants:
    """Explicitly verifies the coding subsystem does not introduce dangerous primitives."""

    def test_no_subprocess_in_coding_modules(self) -> None:
        """Static inspection proving no subprocess or shell primitives exist in coding modules."""
        import baby.agents.coding.agent as agent_mod
        import baby.tools.coding.list_files as list_mod
        import baby.tools.coding.path_resolver as resolver_mod
        import baby.tools.coding.read_file as read_mod
        import baby.tools.coding.write_file as write_mod

        modules = [agent_mod, list_mod, read_mod, write_mod, resolver_mod]

        import ast

        for mod in modules:
            source = inspect.getsource(mod)
            tree = ast.parse(source)

            # Check imports
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name != "subprocess", f"subprocess imported in {mod.__name__}"
                elif isinstance(node, ast.ImportFrom):
                    assert node.module != "subprocess", f"subprocess imported in {mod.__name__}"
                elif isinstance(node, ast.Attribute):
                    assert node.attr not in (
                        "system",
                        "popen",
                        "spawn",
                    ), f"Forbidden attribute call {node.attr} in {mod.__name__}"

    def test_no_generic_run_command_tool(self) -> None:
        """Ensure no 'run_command' or terminal execution tool is registered or implemented."""
        from baby.tools import tool_registry

        tools = tool_registry.list_tools()
        tool_names = [t.name for t in tools]
        assert "run_command" not in tool_names
        assert "execute_command" not in tool_names
        assert "terminal" not in tool_names
        assert "shell" not in tool_names
