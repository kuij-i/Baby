"""Abstract model provider interface.

All model provider implementations must conform to this interface.
Provider-specific details (SDK calls, auth, response parsing) are
encapsulated behind this abstraction so the orchestration and agent
layers never depend on a particular LLM vendor.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from baby.providers.types import ChatMessage, ProviderResponse, ToolDefinition


class ModelProvider(ABC):
    """Abstract interface for LLM model providers.

    Implementations wrap vendor-specific SDKs (OpenAI, Anthropic, local
    servers, etc.) and expose a uniform completion API.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider name (e.g. 'openai', 'anthropic', 'local')."""

    @property
    @abstractmethod
    def supported_models(self) -> List[str]:
        """Model identifiers this provider can serve."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the provider is configured and ready for use.

        This is a local configuration/credential check and does not make network calls.
        """

    @abstractmethod
    async def complete(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stop: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> ProviderResponse:
        """Generate a completion from a list of chat messages.

        Args:
            messages: Conversation history.
            model: Model identifier. If None, use provider default.
            temperature: Sampling temperature (0.0–2.0).
            max_tokens: Maximum tokens in the response.
            stop: Stop sequences.
            **kwargs: Provider-specific options.

        Returns:
            Normalized ProviderResponse.

        Raises:
            ExecutionError: On provider failure.
        """

    @abstractmethod
    async def complete_with_tools(
        self,
        messages: List[ChatMessage],
        tools: List[ToolDefinition],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> ProviderResponse:
        """Generate a completion that may invoke tool/function calls.

        Args:
            messages: Conversation history.
            tools: Available tool definitions.
            model: Model identifier. If None, use provider default.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens in the response.
            **kwargs: Provider-specific options.

        Returns:
            Normalized ProviderResponse (may contain tool_calls).

        Raises:
            ExecutionError: On provider failure.
        """

    def get_info(self) -> Dict[str, Any]:
        """Return metadata about this provider for diagnostics."""
        return {
            "name": self.name,
            "supported_models": self.supported_models,
            "available": self.is_available(),
        }
