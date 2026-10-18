"""Provider registry for model provider lookup and management."""

from typing import Any, Dict, List, Optional

from baby.errors import ConfigurationError
from baby.logging import get_logger
from baby.providers.base import ModelProvider

logger = get_logger(__name__)


class ProviderRegistry:
    """Registry of available model providers.

    Allows registration, lookup, and enumeration of providers.
    Thread-safety is not required for the current single-process architecture.
    """

    def __init__(self) -> None:
        self._providers: Dict[str, ModelProvider] = {}
        self._default_provider: Optional[str] = None

    def register(self, provider: ModelProvider, *, default: bool = False) -> None:
        """Register a model provider.

        Args:
            provider: The provider instance to register.
            default: If True, set this as the default provider.

        Raises:
            ValueError: If a provider with the same name is already registered.
        """
        if provider.name in self._providers:
            raise ValueError(f"Provider '{provider.name}' is already registered")
        self._providers[provider.name] = provider
        logger.info("Provider registered", provider=provider.name, default=default)
        if default or self._default_provider is None:
            self._default_provider = provider.name

    def unregister(self, name: str) -> None:
        """Remove a provider from the registry.

        Raises:
            ConfigurationError: If the provider is not registered.
        """
        if name not in self._providers:
            raise ConfigurationError(f"Provider '{name}' is not registered")
        del self._providers[name]
        if self._default_provider == name:
            self._default_provider = next(iter(self._providers), None)
        logger.info("Provider unregistered", provider=name)

    def get(self, name: str) -> ModelProvider:
        """Retrieve a provider by name.

        Raises:
            ConfigurationError: If the provider is not registered.
        """
        if name not in self._providers:
            raise ConfigurationError(f"Provider '{name}' is not registered")
        return self._providers[name]

    def get_default(self) -> ModelProvider:
        """Return the default provider.

        Raises:
            ConfigurationError: If no providers are registered.
        """
        if self._default_provider is None:
            raise ConfigurationError("No providers registered")
        return self._providers[self._default_provider]

    def list_providers(self) -> List[ModelProvider]:
        """Return all registered providers."""
        return list(self._providers.values())

    def get_provider_for_model(self, model: str) -> Optional[ModelProvider]:
        """Find a provider that supports the given model identifier."""
        for provider in self._providers.values():
            if model in provider.supported_models:
                return provider
        return None

    def clear(self) -> None:
        """Remove all providers. Intended for testing."""
        self._providers.clear()
        self._default_provider = None


# Global provider registry instance
provider_registry = ProviderRegistry()


def active_provider_name(cfg: Optional[Any] = None) -> Optional[str]:
    """Name of the provider workers should use (``"nvidia"``/``"openai"``), or None if none is configured.

    ``model_provider="auto"`` prefers NVIDIA when ``NVIDIA_API_KEY`` is set, then OpenAI when
    ``OPENAI_API_KEY`` is set. An explicit choice is honoured only if its key is configured.
    """
    from baby.configuration import settings

    cfg = cfg or settings
    has_nvidia = bool((cfg.nvidia_api_key or "").strip())
    has_openai = bool((cfg.openai_api_key or "").strip())
    choice = (cfg.model_provider or "auto").strip().lower()
    if choice == "nvidia":
        return "nvidia" if has_nvidia else None
    if choice == "openai":
        return "openai" if has_openai else None
    if has_nvidia:
        return "nvidia"
    if has_openai:
        return "openai"
    return None


def configured_model(cfg: Optional[Any] = None) -> str:
    """Model identifier agents should request from the active provider."""
    from baby.configuration import settings

    cfg = cfg or settings
    return str(cfg.nvidia_model if active_provider_name(cfg) == "nvidia" else cfg.default_model)


def register_configured_providers(registry: Optional[ProviderRegistry] = None) -> List[str]:
    """Register the model provider selected by configuration; return the registered names.

    The NVIDIA provider (``NVIDIA_API_KEY``, optional ``NVIDIA_BASE_URL`` / ``NVIDIA_MODEL``)
    or the OpenAI-compatible provider (``OPENAI_API_KEY``, optional ``OPENAI_BASE_URL`` /
    ``DEFAULT_MODEL``), chosen by ``MODEL_PROVIDER`` (see ``active_provider_name``). Without
    credentials nothing is registered and agents fall back to their provider-less behaviour.
    Idempotent.
    """
    from baby.providers.nvidia_provider import NvidiaProvider
    from baby.providers.openai_provider import OpenAIProvider

    registry = registry or provider_registry
    active = active_provider_name()
    if active is None:
        return []
    provider: ModelProvider = NvidiaProvider() if active == "nvidia" else OpenAIProvider()
    if provider.name not in {p.name for p in registry.list_providers()}:
        registry.register(provider, default=True)
    return [provider.name]
