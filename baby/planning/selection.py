"""Agent and tool selection logic for orchestration."""

from typing import List, Optional

from baby.agents import agent_registry
from baby.core import (
    AgentSpec,
    PermissionCategory,
    PlanStep,
    Task,
    ToolSpec,
)
from baby.logging import get_logger
from baby.tools import tool_registry

logger = get_logger(__name__)


class SelectionLogic:
    """Selects best agent and tool for a task step.

    Uses capability matching to find appropriate agents and tools.
    """

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
                agent = agent_registry.get(str(step.agent_id))
                return agent.spec
            except Exception as e:
                logger.warning(f"Specified agent not found: {e}")
                return None

        # Otherwise, get first enabled agent
        agents = agent_registry.list_agents()
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
                tool = tool_registry.get(step.tool_name)
                return tool.spec
            except Exception as e:
                logger.warning(f"Specified tool not found: {e}")
                return None

        # Otherwise, get first available tool
        tools = tool_registry.list_tools()

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
        agents = agent_registry.get_agents_with_capability(capability_name)
        return [a.spec for a in agents]

    def get_tools_requiring_permission(self, permission: PermissionCategory) -> List[ToolSpec]:
        """Get all tools requiring a permission.

        Args:
            permission: Permission category

        Returns:
            List of tool specs
        """
        tools = tool_registry.get_tools_by_permission(permission.value)
        return [t.spec for t in tools]


# Global selection logic instance
selection_logic = SelectionLogic()
