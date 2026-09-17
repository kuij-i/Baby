"""Agent framework for BABY.

Provides base agent class, registry, and agent management.
"""

from baby.agents.base import Agent
from baby.agents.registry import AgentRegistry, agent_registry

__all__ = ["Agent", "agent_registry", "AgentRegistry"]
