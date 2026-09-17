"""Agent framework for BABY.

Provides base classes and interfaces for all agent types.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict

from baby.core import AgentResult, AgentSpec, ExecutionContext


class Agent(ABC):
    """Base class for all agents in BABY.

    All specialist agents (Coding, Trading, Copywriting, Testing, Cybersecurity)
    inherit from this base class.
    """

    def __init__(self, spec: AgentSpec) -> None:
        """Initialize agent with specification.

        Args:
            spec: Agent specification with capabilities, permissions, etc.
        """
        self.spec = spec
        self._metadata: Dict[str, Any] = {}

    @property
    def id(self) -> str:
        """Get agent ID."""
        return str(self.spec.id)

    @property
    def name(self) -> str:
        """Get agent name."""
        return self.spec.name

    @property
    def role(self) -> str:
        """Get agent role."""
        return self.spec.role

    @abstractmethod
    async def execute(self, context: ExecutionContext) -> AgentResult:
        """Execute a plan step.

        Args:
            context: Execution context with task, step, and previous results

        Returns:
            Agent result with success/failure and output

        Raises:
            ExecutionError: If execution fails
        """
        pass

    def get_metadata(self, key: str, default: Any = None) -> Any:
        """Get metadata about agent execution.

        Args:
            key: Metadata key
            default: Default value if key not found

        Returns:
            Metadata value or default
        """
        return self._metadata.get(key, default)

    def set_metadata(self, key: str, value: Any) -> None:
        """Set metadata about agent execution.

        Args:
            key: Metadata key
            value: Metadata value
        """
        self._metadata[key] = value
