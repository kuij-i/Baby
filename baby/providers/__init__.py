"""Model provider subsystem.

Exports the abstract interface, shared types, the OpenAI-compatible
and NVIDIA concrete providers, and the global provider registry.
"""

from baby.providers.base import ModelProvider
from baby.providers.nvidia_provider import NvidiaProvider
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
    "NvidiaProvider",
    "OpenAIProvider",
    "ProviderRegistry",
    "ProviderResponse",
    "TokenUsage",
    "ToolDefinition",
    "provider_registry",
]
