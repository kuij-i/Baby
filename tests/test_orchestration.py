"""Tests for orchestration system."""

import pytest
from datetime import datetime

from baby.planning.planner import task_planner
from baby.planning.selection import selection_logic
from baby.orchestration.orchestrator import orchestrator
from baby.core import (
    Task,
    AgentId,
    AgentSpec,
    AgentCapability,
    ToolSpec,
    ToolPermission,
    PermissionCategory,
    PermissionLevel,
    PlanStep,
)
from baby.agents import agent_registry, Agent
from baby.tools import tool_registry, Tool, ToolResult
from baby.permissions import permission_manager


class MockAgent(Agent):
    """Mock agent for testing."""

    async def execute(self, context) -> 'AgentResult':
        from baby.core import AgentResult
        return AgentResult(
            step_id=context.plan_step.step_id,
            agent_id=context.agent.id,
            success=True,
            output={"result": "mock"},
        )


class MockTool(Tool):
    """Mock tool for testing."""

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(
            success=True,
            output={"result": "mock"},
            execution_time_ms=5,
        )


class TestTaskPlanner:
    """Tests for TaskPlanner."""

    @pytest.mark.asyncio
    async def test_plan_simple_task(self) -> None:
        """Test planning a simple task."""
        task = Task(
            title="Test Task",
            description="Execute a test step",
            priority=5,
        )

        plan = await task_planner.plan(task)

        assert plan.task_id == task.id
        assert len(plan.steps) >= 1
        assert plan.steps[0].description == task.description

    @pytest.mark.asyncio
    async def test_plan_high_priority_task(self) -> None:
        """Test planning high priority task requires approval."""
        task = Task(
            title="High Priority Task",
            description="Urgent execution",
            priority=9,
        )

        plan = await task_planner.plan(task)

        # High priority should mark for approval
        assert any(step.approval_required for step in plan.steps)

    @pytest.mark.asyncio
    async def test_plan_low_priority_task(self) -> None:
        """Test planning low priority task doesn't require approval."""
        task = Task(
            title="Low Priority Task",
            description="Standard execution",
            priority=2,
        )

        plan = await task_planner.plan(task)

        # Low priority should not require approval
        assert not any(step.approval_required for step in plan.steps)


class TestSelectionLogic:
    """Tests for SelectionLogic."""

    def setup_method(self) -> None:
        """Setup test environment."""
        agent_registry.clear()
        tool_registry.clear()
        permission_manager.clear()

    @pytest.mark.asyncio
    async def test_select_specified_agent(self) -> None:
        """Test selecting specified agent."""
        agent_id = AgentId(id="test-agent")
        spec = AgentSpec(
            id=agent_id,
            name="Test Agent",
            description="Test",
            role="tester",
        )
        agent = MockAgent(spec)
        agent_registry.register(agent)

        task = Task(title="Test", description="Test")
        step = PlanStep(
            step_id=1,
            description="Test step",
            agent_id=agent_id,
        )

        selected = await selection_logic.select_agent(step, task)
        assert selected is not None
        assert selected.id == agent_id

    @pytest.mark.asyncio
    async def test_select_any_agent(self) -> None:
        """Test selecting any available agent."""
        agent_id = AgentId(id="test-agent")
        spec = AgentSpec(
            id=agent_id,
            name="Test Agent",
            description="Test",
            role="tester",
        )
        agent = MockAgent(spec)
        agent_registry.register(agent)

        task = Task(title="Test", description="Test")
        step = PlanStep(
            step_id=1,
            description="Test step",
        )

        selected = await selection_logic.select_agent(step, task)
        assert selected is not None

    @pytest.mark.asyncio
    async def test_select_disabled_agent_skipped(self) -> None:
        """Test that disabled agents are skipped."""
        agent_id1 = AgentId(id="disabled-agent")
        spec1 = AgentSpec(
            id=agent_id1,
            name="Disabled Agent",
            description="Test",
            role="tester",
            enabled=False,
        )
        agent1 = MockAgent(spec1)
        agent_registry.register(agent1)

        agent_id2 = AgentId(id="enabled-agent")
        spec2 = AgentSpec(
            id=agent_id2,
            name="Enabled Agent",
            description="Test",
            role="tester",
            enabled=True,
        )
        agent2 = MockAgent(spec2)
        agent_registry.register(agent2)

        task = Task(title="Test", description="Test")
        step = PlanStep(
            step_id=1,
            description="Test step",
        )

        selected = await selection_logic.select_agent(step, task)
        assert selected is not None
        assert selected.id == agent_id2

    @pytest.mark.asyncio
    async def test_select_specified_tool(self) -> None:
        """Test selecting specified tool."""
        perm = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read",
        )
        spec = ToolSpec(
            name="test-tool",
            description="Test",
            permissions_required=[perm],
            input_schema={},
        )
        tool = MockTool(spec)
        tool_registry.register(tool)

        task = Task(title="Test", description="Test")
        step = PlanStep(
            step_id=1,
            description="Test step",
            tool_name="test-tool",
        )

        selected = await selection_logic.select_tool(step, task)
        assert selected is not None
        assert selected.name == "test-tool"

    @pytest.mark.asyncio
    async def test_select_any_tool(self) -> None:
        """Test selecting any available tool."""
        perm = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read",
        )
        spec = ToolSpec(
            name="test-tool",
            description="Test",
            permissions_required=[perm],
            input_schema={},
        )
        tool = MockTool(spec)
        tool_registry.register(tool)

        task = Task(title="Test", description="Test")
        step = PlanStep(
            step_id=1,
            description="Test step",
        )

        selected = await selection_logic.select_tool(step, task)
        assert selected is not None

    @pytest.mark.asyncio
    async def test_get_agents_with_capability(self) -> None:
        """Test getting agents with capability."""
        cap = AgentCapability(
            name="code-review",
            description="Review code",
        )
        agent_id = AgentId(id="test-agent")
        spec = AgentSpec(
            id=agent_id,
            name="Test Agent",
            description="Test",
            role="coder",
            capabilities=[cap],
        )
        agent = MockAgent(spec)
        agent_registry.register(agent)

        agents = selection_logic.get_agents_with_capability("code-review")
        assert len(agents) == 1
        assert agents[0].id == agent_id

    @pytest.mark.asyncio
    async def test_get_tools_by_permission(self) -> None:
        """Test getting tools by permission."""
        perm = ToolPermission(
            category=PermissionCategory.LOCAL_WRITE,
            level=PermissionLevel.ALLOW,
            description="Write",
        )
        spec = ToolSpec(
            name="test-tool",
            description="Test",
            permissions_required=[perm],
            input_schema={},
        )
        tool = MockTool(spec)
        tool_registry.register(tool)

        tools = selection_logic.get_tools_requiring_permission(PermissionCategory.LOCAL_WRITE)
        assert len(tools) == 1
        assert tools[0].name == "test-tool"


class TestOrchestrator:
    """Tests for Orchestrator."""

    def setup_method(self) -> None:
        """Setup test environment."""
        agent_registry.clear()
        tool_registry.clear()
        permission_manager.clear()

    @pytest.mark.asyncio
    async def test_execute_simple_task(self) -> None:
        """Test executing a simple task."""
        # Setup agent
        agent_id = AgentId(id="test-agent")
        spec = AgentSpec(
            id=agent_id,
            name="Test Agent",
            description="Test",
            role="tester",
        )
        agent = MockAgent(spec)
        agent_registry.register(agent)

        # Execute task
        task = Task(
            title="Test Task",
            description="Test execution",
            priority=5,
        )

        result = await orchestrator.execute_task(task)
        assert result is not None
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_task_no_agent(self) -> None:
        """Test executing task with no available agent."""
        task = Task(
            title="Test Task",
            description="Test execution",
            priority=5,
        )

        with pytest.raises(Exception):
            await orchestrator.execute_task(task)

    @pytest.mark.asyncio
    async def test_execute_task_records_audit(self) -> None:
        """Test that task execution records audit events."""
        # Setup agent
        agent_id = AgentId(id="test-agent")
        spec = AgentSpec(
            id=agent_id,
            name="Test Agent",
            description="Test",
            role="tester",
        )
        agent = MockAgent(spec)
        agent_registry.register(agent)

        # Execute task
        task = Task(
            title="Test Task",
            description="Test execution",
            priority=5,
        )

        result = await orchestrator.execute_task(task, user_id="test-user")
        assert result is not None

        # Check audit events were recorded
        from baby.audit import audit_log
        from baby.core import AuditEventType
        
        events = audit_log.get_events_by_task(task.id)
        assert len(events) > 0
        
        event_types = [e.event_type for e in events]
        assert AuditEventType.TASK_CREATED in event_types
        assert AuditEventType.PLAN_CREATED in event_types
