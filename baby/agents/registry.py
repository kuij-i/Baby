"""Agent registry for managing all agents in the system."""

from typing import Dict, Optional, List
from baby.agents.base import Agent
from baby.core import AgentId
from baby.errors import AgentNotFoundError


class AgentRegistry:
    """Registry for all agents in BABY.
    
    Provides central management and lookup of agents by ID, role, or capability.
    """

    def __init__(self) -> None:
        """Initialize empty agent registry."""
        self._agents: Dict[str, Agent] = {}

    def register(self, agent: Agent) -> None:
        """Register an agent.
        
        Args:
            agent: Agent instance to register
            
        Raises:
            ValueError: If agent is already registered
        """
        agent_id = agent.spec.id.id
        if agent_id in self._agents:
            raise ValueError(f"Agent {agent_id} is already registered")
        
        self._agents[agent_id] = agent

    def unregister(self, agent_id: str) -> None:
        """Unregister an agent.
        
        Args:
            agent_id: ID of agent to unregister
            
        Raises:
            AgentNotFoundError: If agent is not registered
        """
        if agent_id not in self._agents:
            raise AgentNotFoundError(f"Agent {agent_id} not registered")
        
        del self._agents[agent_id]

    def get(self, agent_id: str) -> Agent:
        """Get an agent by ID.
        
        Args:
            agent_id: ID of agent to retrieve
            
        Returns:
            Agent instance
            
        Raises:
            AgentNotFoundError: If agent is not registered
        """
        if agent_id not in self._agents:
            raise AgentNotFoundError(f"Agent {agent_id} not registered")
        
        return self._agents[agent_id]

    def list_agents(self) -> List[Agent]:
        """List all registered agents.
        
        Returns:
            List of all agents
        """
        return list(self._agents.values())

    def get_agents_by_role(self, role: str) -> List[Agent]:
        """Get all agents with a specific role.
        
        Args:
            role: Role to filter by
            
        Returns:
            List of agents with the role
        """
        return [agent for agent in self._agents.values() if agent.role == role]

    def get_agents_with_capability(self, capability_name: str) -> List[Agent]:
        """Get all agents with a specific capability.
        
        Args:
            capability_name: Capability name to filter by
            
        Returns:
            List of agents with the capability
        """
        return [
            agent
            for agent in self._agents.values()
            if any(cap.name == capability_name for cap in agent.spec.capabilities)
        ]

    def clear(self) -> None:
        """Clear all agents (for testing only)."""
        self._agents.clear()


# Global agent registry instance
agent_registry = AgentRegistry()
