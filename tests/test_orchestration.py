"""Tests for orchestration system."""

import pytest

from baby.agents import Agent, agent_registry, register_coding_agent
from baby.audit import audit_log
from baby.core import (
    AgentCapability,
    AgentId,
    AgentResult,
    AgentSpec,
    AuditEventType,
    PermissionCategory,
    PermissionLevel,
    Plan,
    PlanStep,
    Task,
    ToolPermission,
    ToolSpec,
)
from baby.memory import memory_store
from baby.orchestration.orchestrator import orchestrator
from baby.permissions import approval_manager, permission_manager
from baby.planning.planner import task_planner
from baby.planning.selection import selection_logic
from baby.tools import Tool, ToolResult, register_safe_coding_tools, tool_registry


class MockAgent(Agent):
    """Mock agent for testing."""

    async def execute(self, context) -> AgentResult:
        return AgentResult(
            step_id=context.plan_step.step_id,
            agent_id=context.agent.id,
            success=True,
            output={"result": "mock"},
        )


class MockTool(Tool):
    """Mock tool for testing."""

    async def execute(self, **kwargs) -> ToolResult:
        return ToolResult(success=True, output={"result": "mock"}, execution_time_ms=5)


class TestTaskPlanner:
    @pytest.mark.asyncio
    async def test_plan_simple_task(self) -> None:
        task = Task(title="Test Task", description="Execute a test step", priority=5)
        plan = await task_planner.plan(task)
        assert plan.task_id == task.id
        assert len(plan.steps) >= 1
        assert plan.steps[0].description == task.description

    @pytest.mark.asyncio
    async def test_plan_high_priority_task(self) -> None:
        task = Task(title="High Priority Task", description="Urgent execution", priority=9)
        plan = await task_planner.plan(task)
        assert any(step.approval_required for step in plan.steps)

    @pytest.mark.asyncio
    async def test_plan_low_priority_task(self) -> None:
        task = Task(title="Low Priority Task", description="Standard execution", priority=2)
        plan = await task_planner.plan(task)
        assert not any(step.approval_required for step in plan.steps)


class TestSelectionLogic:
    def setup_method(self) -> None:
        agent_registry.clear()
        tool_registry.clear()
        permission_manager.clear()
        approval_manager.clear()

    @pytest.mark.asyncio
    async def test_select_specified_agent(self) -> None:
        agent_id = AgentId(id="test-agent")
        agent_registry.register(MockAgent(AgentSpec(id=agent_id, name="Test Agent", description="Test", role="tester")))
        selected = await selection_logic.select_agent(
            PlanStep(step_id=1, description="Test step", agent_id=agent_id),
            Task(title="Test", description="Test"),
        )
        assert selected is not None
        assert selected.id == agent_id

    @pytest.mark.asyncio
    async def test_select_any_agent(self) -> None:
        agent_id = AgentId(id="test-agent")
        agent_registry.register(MockAgent(AgentSpec(id=agent_id, name="Test Agent", description="Test", role="tester")))
        selected = await selection_logic.select_agent(
            PlanStep(step_id=1, description="Test step"), Task(title="Test", description="Test")
        )
        assert selected is not None

    @pytest.mark.asyncio
    async def test_select_disabled_agent_skipped(self) -> None:
        agent_registry.register(
            MockAgent(
                AgentSpec(
                    id=AgentId(id="disabled-agent"),
                    name="Disabled Agent",
                    description="Test",
                    role="tester",
                    enabled=False,
                )
            )
        )
        enabled_id = AgentId(id="enabled-agent")
        agent_registry.register(
            MockAgent(AgentSpec(id=enabled_id, name="Enabled Agent", description="Test", role="tester"))
        )
        selected = await selection_logic.select_agent(
            PlanStep(step_id=1, description="Test step"), Task(title="Test", description="Test")
        )
        assert selected is not None
        assert selected.id == enabled_id

    @pytest.mark.asyncio
    async def test_select_specified_tool(self) -> None:
        permission = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read",
        )
        tool_registry.register(
            MockTool(ToolSpec(name="test-tool", description="Test", permissions_required=[permission], input_schema={}))
        )
        selected = await selection_logic.select_tool(
            PlanStep(step_id=1, description="Test step", tool_name="test-tool"),
            Task(title="Test", description="Test"),
        )
        assert selected is not None
        assert selected.name == "test-tool"

    @pytest.mark.asyncio
    async def test_select_coding_agent_for_coding_task(self, tmp_path) -> None:
        """Test coding tasks route to the registered coding agent."""
        register_safe_coding_tools(tmp_path)
        register_coding_agent(tmp_path)
        task = Task(
            title="Read code file",
            description="Read a repository file",
            metadata={"domain": "coding", "operation": "read_file", "path": "README.md"},
        )
        plan = await task_planner.plan(task)

        selected = await selection_logic.select_agent(plan.steps[0], task)

        assert selected is not None
        assert selected.id == AgentId(id="coding-agent")

    @pytest.mark.asyncio
    async def test_select_any_tool(self) -> None:
        permission = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read",
        )
        tool_registry.register(
            MockTool(ToolSpec(name="test-tool", description="Test", permissions_required=[permission], input_schema={}))
        )
        selected = await selection_logic.select_tool(
            PlanStep(step_id=1, description="Test step"), Task(title="Test", description="Test")
        )
        assert selected is not None

    def test_get_agents_with_capability(self) -> None:
        capability = AgentCapability(name="code-review", description="Review code")
        agent_id = AgentId(id="test-agent")
        agent_registry.register(
            MockAgent(
                AgentSpec(
                    id=agent_id,
                    name="Test Agent",
                    description="Test",
                    role="coder",
                    capabilities=[capability],
                )
            )
        )
        agents = selection_logic.get_agents_with_capability("code-review")
        assert len(agents) == 1
        assert agents[0].id == agent_id

    def test_get_tools_by_permission(self) -> None:
        permission = ToolPermission(
            category=PermissionCategory.LOCAL_WRITE,
            level=PermissionLevel.ALLOW,
            description="Write",
        )
        tool_registry.register(
            MockTool(ToolSpec(name="test-tool", description="Test", permissions_required=[permission], input_schema={}))
        )
        tools = selection_logic.get_tools_requiring_permission(PermissionCategory.LOCAL_WRITE)
        assert len(tools) == 1
        assert tools[0].name == "test-tool"


class TestOrchestrator:
    def setup_method(self) -> None:
        agent_registry.clear()
        tool_registry.clear()
        permission_manager.clear()
        approval_manager.clear()
        audit_log.clear()
        memory_store.clear()

    @pytest.mark.asyncio
    async def test_execute_simple_task(self) -> None:
        agent_id = AgentId(id="test-agent")
        agent_registry.register(MockAgent(AgentSpec(id=agent_id, name="Test Agent", description="Test", role="tester")))
        result = await orchestrator.execute_task(Task(title="Test Task", description="Test execution"))
        assert result is not None
        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_task_without_agent_returns_failure(self) -> None:
        result = await orchestrator.execute_task(Task(title="Test Task", description="Test execution"))
        assert result is not None
        assert result.success is False
        assert result.error == "No enabled agent available"

    @pytest.mark.asyncio
    async def test_execute_task_records_audit(self) -> None:
        task = Task(title="Test Task", description="Test execution")
        agent_id = AgentId(id="test-agent")
        agent_registry.register(MockAgent(AgentSpec(id=agent_id, name="Test Agent", description="Test", role="tester")))
        result = await orchestrator.execute_task(task, user_id="test-user")
        assert result is not None
        events = audit_log.get_events_by_task(task.id)
        assert len(events) > 0
        event_types = [event.event_type for event in events]
        assert AuditEventType.TASK_CREATED in event_types
        assert AuditEventType.PLAN_CREATED in event_types
        assert AuditEventType.VERIFICATION_COMPLETED in event_types

    @pytest.mark.asyncio
    async def test_execute_coding_task_requires_approval_for_write(self, tmp_path) -> None:
        """Test approval-required coding writes do not execute before explicit approval."""
        register_safe_coding_tools(tmp_path)
        register_coding_agent(tmp_path)
        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_WRITE,
                level=PermissionLevel.APPROVAL_REQUIRED,
                description="Write with approval",
            ),
        )
        task = Task(
            title="Write repository file",
            description="Write a repository file",
            metadata={
                "domain": "coding",
                "operation": "write_file",
                "path": "notes.txt",
                "content": "hello",
            },
        )

        result = await orchestrator.execute_task(task)

        assert result is not None
        assert result.success is False
        assert "Approval required" in (result.error or "")
        assert not (tmp_path / "notes.txt").exists()
        event_types = [event.event_type for event in audit_log.get_events(task_id=task.id)]
        assert AuditEventType.APPROVAL_REQUESTED in event_types

    @pytest.mark.asyncio
    async def test_execute_coding_task_after_approval_succeeds_and_stores_memory(self, tmp_path) -> None:
        """Test approved coding writes execute, verify, and store safe memory."""
        register_safe_coding_tools(tmp_path)
        register_coding_agent(tmp_path)
        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_WRITE,
                level=PermissionLevel.APPROVAL_REQUIRED,
                description="Write with approval",
            ),
        )
        task = Task(
            title="Write repository file",
            description="Write a repository file",
            metadata={
                "domain": "coding",
                "operation": "write_file",
                "path": "notes.txt",
                "content": "approved",
            },
        )

        first_attempt = await orchestrator.execute_task(task)
        assert first_attempt is not None
        request = approval_manager.find_request(
            task_id=task.id,
            agent_id=AgentId(id="coding-agent"),
            action_type="tool:repository_write_file:local_write",
        )
        assert request is not None
        approval_manager.decide(request.request_id, approved=True)

        result = await orchestrator.execute_task(task)

        assert result is not None
        assert result.success is True
        assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "approved"
        records = memory_store.search(task_id=task.id, record_type="coding_action")
        assert len(records) == 1
        assert records[0].content["paths"] == ["notes.txt"]

    @pytest.mark.asyncio
    async def test_failed_dependency_prevents_dependent_step_execution(self) -> None:
        """Test failed dependencies stop dependent steps and pass previous results only when ready."""

        class FailingAgent(Agent):
            async def execute(self, context) -> AgentResult:
                return AgentResult(
                    step_id=context.plan_step.step_id,
                    agent_id=context.agent.id,
                    success=False,
                    error="boom",
                )

        class DependentAgent(Agent):
            async def execute(self, context) -> AgentResult:
                pytest.fail("Dependent step should not execute after a failed dependency")

        primary_id = AgentId(id="primary-agent")
        dependent_id = AgentId(id="dependent-agent")
        agent_registry.register(
            FailingAgent(AgentSpec(id=primary_id, name="Primary", description="Test", role="tester"))
        )
        agent_registry.register(
            DependentAgent(AgentSpec(id=dependent_id, name="Dependent", description="Test", role="tester"))
        )

        original_plan = task_planner.plan

        async def fake_plan(task: Task):
            return Plan(
                task_id=task.id,
                reasoning="dependency test",
                steps=[
                    PlanStep(step_id=1, description="step 1", agent_id=primary_id),
                    PlanStep(step_id=2, description="step 2", agent_id=dependent_id, dependencies=[1]),
                ],
            )

        task_planner.plan = fake_plan
        try:
            result = await orchestrator.execute_task(Task(title="Dependency task", description="Test dependencies"))
        finally:
            task_planner.plan = original_plan

        assert result is not None
        assert result.success is False
        assert result.error == "boom"

    @pytest.mark.asyncio
    async def test_previous_results_are_propagated_to_dependencies(self) -> None:
        """Test dependency results are passed into dependent execution contexts."""

        class PrimaryAgent(Agent):
            async def execute(self, context) -> AgentResult:
                return AgentResult(step_id=1, agent_id=context.agent.id, success=True, output={"value": "seed"})

        observed: dict[int, AgentResult] = {}

        class DependentAgent(Agent):
            async def execute(self, context) -> AgentResult:
                observed.update(context.previous_results)
                return AgentResult(step_id=2, agent_id=context.agent.id, success=True, output={"value": "done"})

        primary_id = AgentId(id="primary-agent")
        dependent_id = AgentId(id="dependent-agent")
        agent_registry.register(
            PrimaryAgent(AgentSpec(id=primary_id, name="Primary", description="Test", role="tester"))
        )
        agent_registry.register(
            DependentAgent(AgentSpec(id=dependent_id, name="Dependent", description="Test", role="tester"))
        )

        original_plan = task_planner.plan

        async def fake_plan(task: Task):
            return Plan(
                task_id=task.id,
                reasoning="dependency test",
                steps=[
                    PlanStep(step_id=1, description="step 1", agent_id=primary_id),
                    PlanStep(step_id=2, description="step 2", agent_id=dependent_id, dependencies=[1]),
                ],
            )

        task_planner.plan = fake_plan
        try:
            result = await orchestrator.execute_task(Task(title="Dependency task", description="Test dependencies"))
        finally:
            task_planner.plan = original_plan

        assert result is not None
        assert result.success is True
        assert 1 in observed
        assert observed[1].output == {"value": "seed"}

    @pytest.mark.asyncio
    async def test_invalid_dependency_reference_raises(self) -> None:
        """Test the orchestrator rejects invalid dependency references."""
        original_plan = task_planner.plan

        async def fake_plan(task: Task):
            return Plan(
                task_id=task.id,
                reasoning="bad dependency",
                steps=[PlanStep(step_id=1, description="step 1", dependencies=[99])],
            )

        task_planner.plan = fake_plan
        try:
            with pytest.raises(ValueError):
                await orchestrator.execute_task(Task(title="Dependency task", description="Bad dependencies"))
        finally:
            task_planner.plan = original_plan

    @pytest.mark.asyncio
    async def test_circular_dependencies_raise(self) -> None:
        """Test the orchestrator rejects circular plan dependencies."""
        original_plan = task_planner.plan

        async def fake_plan(task: Task):
            return Plan(
                task_id=task.id,
                reasoning="circular dependency",
                steps=[
                    PlanStep(step_id=1, description="step 1", dependencies=[2]),
                    PlanStep(step_id=2, description="step 2", dependencies=[1]),
                ],
            )

        task_planner.plan = fake_plan
        try:
            with pytest.raises(ValueError):
                await orchestrator.execute_task(Task(title="Dependency task", description="Circular dependencies"))
        finally:
            task_planner.plan = original_plan
