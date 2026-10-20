"""Agent and tool selection logic for orchestration."""

from typing import List, Optional

from baby.agents import agent_registry
from baby.agents.registry import AgentRegistry
from baby.core import (
    AgentSpec,
    PermissionCategory,
    PlanStep,
    Task,
    ToolSpec,
)
from baby.logging import get_logger
from baby.tools import tool_registry
from baby.tools.registry import ToolRegistry

logger = get_logger(__name__)


class SelectionLogic:
    """Selects best agent and tool for a task step.

    Uses capability matching to find appropriate agents and tools.
    """

    def __init__(self, *, agents: Optional[AgentRegistry] = None, tools: Optional[ToolRegistry] = None) -> None:
        """Initialize selection logic.

        Args:
            agents: Agent registry to select from; defaults to ``agent_registry``, resolved when used.
            tools: Tool registry to select from; defaults to ``tool_registry``, resolved when used.
        """
        self._agents = agents
        self._tools = tools

    @property
    def agents(self) -> AgentRegistry:
        """Agent registry in use."""
        return self._agents if self._agents is not None else agent_registry

    @property
    def tools(self) -> ToolRegistry:
        """Tool registry in use."""
        return self._tools if self._tools is not None else tool_registry

    async def select_agent(self, step: PlanStep, task: Task) -> Optional[AgentSpec]:
        """Select best agent for a step.

        Args:
            step: Plan step
            task: Associated task

        Returns:
            Best matching agent spec or None
        """
        # If agent specified, use that
        if step.agent_id:
            try:
                agent = self.agents.get(str(step.agent_id))
                return agent.spec
            except Exception as e:
                logger.warning(f"Specified agent not found: {e}")
                return None

        # Otherwise, get first enabled agent
        agents = self.agents.list_agents()
        enabled_agents = [a for a in agents if a.spec.enabled]

        if not enabled_agents:
            logger.warning("No enabled agents available")
            return None

        selected = enabled_agents[0]
        logger.info(
            "Selected agent for step",
            agent_id=selected.id,
            step_id=step.step_id,
        )

        return selected.spec

    async def select_tool(self, step: PlanStep, task: Task) -> Optional[ToolSpec]:
        """Select best tool for a step.

        Args:
            step: Plan step
            task: Associated task

        Returns:
            Best matching tool spec or None
        """
        # If tool specified, use that
        if step.tool_name:
            try:
                tool = self.tools.get(step.tool_name)
                return tool.spec
            except Exception as e:
                logger.warning(f"Specified tool not found: {e}")
                return None

        # Otherwise, get first available tool
        tools = self.tools.list_tools()

        if not tools:
            logger.warning("No tools available")
            return None

        selected = tools[0]
        logger.info(
            "Selected tool for step",
            tool=selected.name,
            step_id=step.step_id,
        )

        return selected.spec

    def get_agents_with_capability(self, capability_name: str) -> List[AgentSpec]:
        """Get all agents with a capability.

        Args:
            capability_name: Capability name

        Returns:
            List of agent specs
        """
        agents = self.agents.get_agents_with_capability(capability_name)
        return [a.spec for a in agents]

    def get_tools_requiring_permission(self, permission: PermissionCategory) -> List[ToolSpec]:
        """Get all tools requiring a permission.

        Args:
            permission: Permission category

        Returns:
            List of tool specs
        """
        tools = self.tools.get_tools_by_permission(permission.value)
        return [t.spec for t in tools]


# Global selection logic instance
selection_logic = SelectionLogic()
