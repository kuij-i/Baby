"""Agent framework for BABY.

Provides base agent class, registry, and agent management.
"""

from baby.agents.base import Agent
from baby.agents.registry import agent_registry, AgentRegistry

__all__ = ["Agent", "agent_registry", "AgentRegistry"]
