"""Tests for CodingAgent and security regression guarantees."""

import inspect
import tempfile
from pathlib import Path

import pytest

from baby.agents.coding.agent import (
    CodingAgent,
    create_default_coding_spec,
)
from baby.agents.registry import agent_registry
from baby.audit import audit_log
from baby.core import (
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

    def __init__(self, response_content: str = "Mock response", tool_calls=None) -> None:
        self._content = response_content
        self._tool_calls = tool_calls
        self.recorded_messages = []

    @property
    def name(self) -> str:
        return "mock"

    @property
    def supported_models(self):
        return ["gpt-4"]

    def is_available(self) -> bool:
        return True

    async def complete(self, messages, **kwargs):
        self.recorded_messages = messages
        return ProviderResponse(content=self._content, model="gpt-4")

    async def complete_with_tools(self, messages, tools, **kwargs):
        self.recorded_messages = messages
        return ProviderResponse(
            content=self._content,
            model="gpt-4",
            tool_calls=self._tool_calls,
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
        assert result.tokens_used == 20
        assert len(result.output["tool_results"]) == 1
        assert result.output["tool_results"][0]["tool"] == "read_file"
        assert result.output["tool_results"][0]["result"]["content"] == "print('baby')"


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
