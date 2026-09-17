"""Tests for agent framework."""

import pytest

from baby.agents.base import Agent
from baby.agents.registry import agent_registry
from baby.core import (
    AgentCapability,
    AgentId,
    AgentResult,
    AgentSpec,
    ExecutionContext,
    PlanStep,
    Task,
)
from baby.errors import AgentNotFoundError


class MockAgent(Agent):
    """Mock agent for testing."""

    async def execute(self, context: ExecutionContext) -> AgentResult:
        """Mock execution."""
        return AgentResult(
            step_id=context.plan_step.step_id,
            agent_id=context.agent.id,
            success=True,
            output={"result": "mock"},
        )


class TestAgent:
    """Tests for Agent base class."""

    def test_agent_creation(self) -> None:
        """Test creating an agent."""
        agent_id = AgentId(id="test-agent")
        spec = AgentSpec(
            id=agent_id,
            name="Test Agent",
            description="A test agent",
            role="tester",
        )
        agent = MockAgent(spec)

        assert agent.id == "test-agent"
        assert agent.name == "Test Agent"
        assert agent.role == "tester"

    def test_agent_metadata(self) -> None:
        """Test agent metadata storage."""
        agent_id = AgentId(id="test-agent")
        spec = AgentSpec(
            id=agent_id,
            name="Test Agent",
            description="A test agent",
            role="tester",
        )
        agent = MockAgent(spec)

        agent.set_metadata("status", "ready")
        assert agent.get_metadata("status") == "ready"
        assert agent.get_metadata("missing", "default") == "default"

    @pytest.mark.asyncio
    async def test_agent_execution(self) -> None:
        """Test agent execution."""
        agent_id = AgentId(id="test-agent")
        cap = AgentCapability(
            name="test-capability",
            description="A test capability",
        )
        spec = AgentSpec(
            id=agent_id,
            name="Test Agent",
            description="A test agent",
            role="tester",
            capabilities=[cap],
        )
        agent = MockAgent(spec)

        task = Task(title="Test", description="Test task")
        step = PlanStep(step_id=1, description="Test step")
        context = ExecutionContext(
            task_id=task.id,
            plan_step=step,
            task=task,
            agent=spec,
        )

        result = await agent.execute(context)
        assert result.success is True
        assert result.output["result"] == "mock"


class TestAgentRegistry:
    """Tests for AgentRegistry."""

    def setup_method(self) -> None:
        """Clear registry before each test."""
        agent_registry.clear()

    def create_test_agent(self, agent_id: str) -> Agent:
        """Helper to create a test agent."""
        spec = AgentSpec(
            id=AgentId(id=agent_id),
            name=f"Agent {agent_id}",
            description="A test agent",
            role="tester",
        )
        return MockAgent(spec)

    def test_register_agent(self) -> None:
        """Test registering an agent."""
        agent = self.create_test_agent("agent-1")
        agent_registry.register(agent)

        retrieved = agent_registry.get("agent-1")
        assert retrieved.id == "agent-1"

    def test_register_duplicate_agent(self) -> None:
        """Test registering the same agent twice fails."""
        agent = self.create_test_agent("agent-1")
        agent_registry.register(agent)

        with pytest.raises(ValueError):
            agent_registry.register(agent)

    def test_get_nonexistent_agent(self) -> None:
        """Test getting a non-existent agent."""
        with pytest.raises(AgentNotFoundError):
            agent_registry.get("nonexistent")

    def test_unregister_agent(self) -> None:
        """Test unregistering an agent."""
        agent = self.create_test_agent("agent-1")
        agent_registry.register(agent)
        agent_registry.unregister("agent-1")

        with pytest.raises(AgentNotFoundError):
            agent_registry.get("agent-1")

    def test_unregister_nonexistent_agent(self) -> None:
        """Test unregistering a non-existent agent."""
        with pytest.raises(AgentNotFoundError):
            agent_registry.unregister("nonexistent")

    def test_list_agents(self) -> None:
        """Test listing all agents."""
        agent1 = self.create_test_agent("agent-1")
        agent2 = self.create_test_agent("agent-2")
        agent_registry.register(agent1)
        agent_registry.register(agent2)

        agents = agent_registry.list_agents()
        assert len(agents) == 2

    def test_get_agents_by_role(self) -> None:
        """Test getting agents by role."""
        spec1 = AgentSpec(
            id=AgentId(id="agent-1"),
            name="Agent 1",
            description="Test",
            role="coder",
        )
        spec2 = AgentSpec(
            id=AgentId(id="agent-2"),
            name="Agent 2",
            description="Test",
            role="tester",
        )
        spec3 = AgentSpec(
            id=AgentId(id="agent-3"),
            name="Agent 3",
            description="Test",
            role="coder",
        )

        agent1 = MockAgent(spec1)
        agent2 = MockAgent(spec2)
        agent3 = MockAgent(spec3)

        agent_registry.register(agent1)
        agent_registry.register(agent2)
        agent_registry.register(agent3)

        coders = agent_registry.get_agents_by_role("coder")
        assert len(coders) == 2

    def test_get_agents_with_capability(self) -> None:
        """Test getting agents with specific capability."""
        cap1 = AgentCapability(
            name="code-review",
            description="Review code",
        )
        cap2 = AgentCapability(
            name="testing",
            description="Write tests",
        )

        spec1 = AgentSpec(
            id=AgentId(id="agent-1"),
            name="Agent 1",
            description="Test",
            role="coder",
            capabilities=[cap1],
        )
        spec2 = AgentSpec(
            id=AgentId(id="agent-2"),
            name="Agent 2",
            description="Test",
            role="tester",
            capabilities=[cap1, cap2],
        )

        agent1 = MockAgent(spec1)
        agent2 = MockAgent(spec2)

        agent_registry.register(agent1)
        agent_registry.register(agent2)

        reviewers = agent_registry.get_agents_with_capability("code-review")
        assert len(reviewers) == 2

        testers = agent_registry.get_agents_with_capability("testing")
        assert len(testers) == 1
