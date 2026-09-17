"""Verification abstractions for BABY agent execution."""

from abc import ABC, abstractmethod

from baby.core import AgentResult, VerificationResult


class Verifier(ABC):
    """Boundary for verifying agent execution results."""

    @abstractmethod
    async def verify(self, result: AgentResult) -> VerificationResult:
        """Verify a single agent result."""
        raise NotImplementedError
