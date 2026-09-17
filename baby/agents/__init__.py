"""Agent framework for BABY.

Provides base agent class, registry, and agent management.
"""
from baby.agents.base import Agent
from baby.agents.coding import (
    CODING_AGENT_ID,
    CODING_CAPABILITY,
    CodingAgent,
    build_coding_agent_spec,
    register_coding_agent,
)
from baby.agents.registry import AgentRegistry, agent_registry

__all__ = [
    "Agent",
    "CodingAgent",
    "CODING_AGENT_ID",
    "CODING_CAPABILITY",
    "build_coding_agent_spec",
    "register_coding_agent",
    "agent_registry",
    "AgentRegistry",
]
