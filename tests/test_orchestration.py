"""Tests for orchestration system."""

import pytest

from baby.agents import Agent, agent_registry
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
    VerificationResult,
)
from baby.orchestration.orchestrator import Orchestrator, orchestrator
from baby.permissions import permission_manager
from baby.planning.planner import task_planner
from baby.planning.selection import selection_logic
from baby.tools import Tool, ToolResult, tool_registry
from baby.verification import Verifier


class MockAgent(Agent):
    """Mock agent for testing."""

    async def execute(self, context) -> AgentResult:
        return AgentResult(
            step_id=context.plan_step.step_id,
            agent_id=context.agent.id,
            success=True,
            output={"result": "mock"},
        )


class TrackingAgent(Agent):
    """Agent that records execution order and dependency context."""

    def __init__(self, spec: AgentSpec, *, succeed: bool = True) -> None:
        super().__init__(spec)
        self.succeed = succeed
        self.seen_contexts = []

    async def execute(self, context) -> AgentResult:
        self.seen_contexts.append(context)
        return AgentResult(
            step_id=context.plan_step.step_id,
            agent_id=context.agent.id,
            success=self.succeed,
            output={"previous_results": sorted(context.previous_results)},
            error=None if self.succeed else "step failed",
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
        audit_log.clear()

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

    @pytest.mark.asyncio
    async def test_execute_task_enforces_dependencies_and_previous_results(self, monkeypatch) -> None:
        task = Task(title="Test Task", description="Test execution")
        first_agent = TrackingAgent(
            AgentSpec(id=AgentId(id="agent-1"), name="Agent 1", description="Test", role="tester")
        )
        second_agent = TrackingAgent(
            AgentSpec(id=AgentId(id="agent-2"), name="Agent 2", description="Test", role="tester")
        )
        agent_registry.register(first_agent)
        agent_registry.register(second_agent)

        async def planned_task(_: Task) -> Plan:
            return Plan(
                task_id=task.id,
                reasoning="dependency test",
                steps=[
                    PlanStep(step_id=2, description="Second", agent_id=second_agent.spec.id, dependencies=[1]),
                    PlanStep(step_id=1, description="First", agent_id=first_agent.spec.id),
                ],
            )

        monkeypatch.setattr(task_planner, "plan", planned_task)

        result = await orchestrator.execute_task(task)

        assert result is not None
        assert result.success is True
        assert [context.plan_step.step_id for context in first_agent.seen_contexts] == [1]
        assert [context.plan_step.step_id for context in second_agent.seen_contexts] == [2]
        assert second_agent.seen_contexts[0].previous_results[1].success is True

    @pytest.mark.asyncio
    async def test_execute_task_blocks_failed_dependencies(self, monkeypatch) -> None:
        task = Task(title="Test Task", description="Test execution")
        failing_agent = TrackingAgent(
            AgentSpec(id=AgentId(id="agent-1"), name="Agent 1", description="Test", role="tester"),
            succeed=False,
        )
        blocked_agent = TrackingAgent(
            AgentSpec(id=AgentId(id="agent-2"), name="Agent 2", description="Test", role="tester")
        )
        agent_registry.register(failing_agent)
        agent_registry.register(blocked_agent)

        async def planned_task(_: Task) -> Plan:
            return Plan(
                task_id=task.id,
                reasoning="dependency failure test",
                steps=[
                    PlanStep(step_id=2, description="Second", agent_id=blocked_agent.spec.id, dependencies=[1]),
                    PlanStep(step_id=1, description="First", agent_id=failing_agent.spec.id),
                ],
            )

        monkeypatch.setattr(task_planner, "plan", planned_task)

        result = await orchestrator.execute_task(task)

        assert result is not None
        assert result.success is False
        assert result.error == "step failed"
        assert blocked_agent.seen_contexts == []

    @pytest.mark.asyncio
    async def test_execute_task_rejects_invalid_dependencies(self, monkeypatch) -> None:
        task = Task(title="Test Task", description="Test execution")
        agent_id = AgentId(id="test-agent")
        agent_registry.register(MockAgent(AgentSpec(id=agent_id, name="Test Agent", description="Test", role="tester")))

        async def planned_task(_: Task) -> Plan:
            return Plan(
                task_id=task.id,
                reasoning="invalid dependency test",
                steps=[PlanStep(step_id=1, description="Only step", agent_id=agent_id, dependencies=[99])],
            )

        monkeypatch.setattr(task_planner, "plan", planned_task)

        with pytest.raises(ValueError, match="invalid dependencies"):
            await orchestrator.execute_task(task)

    @pytest.mark.asyncio
    async def test_execute_task_rejects_circular_dependencies(self, monkeypatch) -> None:
        task = Task(title="Test Task", description="Test execution")
        first_agent = AgentId(id="agent-1")
        second_agent = AgentId(id="agent-2")
        agent_registry.register(MockAgent(AgentSpec(id=first_agent, name="Agent 1", description="Test", role="tester")))
        agent_registry.register(
            MockAgent(AgentSpec(id=second_agent, name="Agent 2", description="Test", role="tester"))
        )

        async def planned_task(_: Task) -> Plan:
            return Plan(
                task_id=task.id,
                reasoning="cycle test",
                steps=[
                    PlanStep(step_id=1, description="First", agent_id=first_agent, dependencies=[2]),
                    PlanStep(step_id=2, description="Second", agent_id=second_agent, dependencies=[1]),
                ],
            )

        monkeypatch.setattr(task_planner, "plan", planned_task)

        with pytest.raises(ValueError, match="Circular dependency"):
            await orchestrator.execute_task(task)

    @pytest.mark.asyncio
    async def test_execute_task_verification_boundary_blocks_unverified_results(self, monkeypatch) -> None:
        task = Task(title="Test Task", description="Test execution")
        agent_id = AgentId(id="test-agent")
        agent_registry.register(MockAgent(AgentSpec(id=agent_id, name="Test Agent", description="Test", role="tester")))

        class RejectingVerifier(Verifier):
            async def verify(self, result: AgentResult) -> VerificationResult:
                return VerificationResult(
                    agent_result_id=result.id,
                    verified=False,
                    verification_method="reject-all",
                    issues=["verification failed"],
                )

        async def planned_task(_: Task) -> Plan:
            return Plan(
                task_id=task.id,
                reasoning="verification test",
                steps=[PlanStep(step_id=1, description="Only step", agent_id=agent_id)],
            )

        monkeypatch.setattr(task_planner, "plan", planned_task)
        local_orchestrator = Orchestrator(verification_boundary=RejectingVerifier())

        result = await local_orchestrator.execute_task(task, user_id="test-user")

        assert result is not None
        assert result.success is False
        assert "Verification failed" in result.error
        verification_events = audit_log.get_events(task_id=task.id, event_type=AuditEventType.VERIFICATION_COMPLETED)
        assert len(verification_events) == 1
        assert verification_events[0].details["verified"] is False
