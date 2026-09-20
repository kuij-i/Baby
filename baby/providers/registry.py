"""Provider registry for model provider lookup and management."""

from typing import Dict, List, Optional

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
