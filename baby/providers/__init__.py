"""Model provider subsystem.

Exports the abstract interface, shared types, the OpenAI-compatible
concrete provider, and the global provider registry.
"""

from baby.providers.base import ModelProvider
from baby.providers.openai_provider import OpenAIProvider
from baby.providers.registry import ProviderRegistry, provider_registry
from baby.providers.types import (
    ChatMessage,
    ProviderResponse,
    TokenUsage,
    ToolDefinition,
)

__all__ = [
    "ChatMessage",
    "ModelProvider",
    "OpenAIProvider",
    "ProviderRegistry",
    "ProviderResponse",
    "TokenUsage",
    "ToolDefinition",
    "provider_registry",
]
