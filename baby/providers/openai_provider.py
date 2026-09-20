"""OpenAI-compatible model provider.

Works with any endpoint that implements the OpenAI chat completions API:
  - OpenAI (api.openai.com)
  - NVIDIA NIM endpoints
  - Local servers (vLLM, llama.cpp, Ollama, LM Studio, etc.)

All configuration is read from Settings — no hard-coded keys.
"""

from typing import Any, Dict, List, Optional

from baby.configuration import settings
from baby.errors import ExecutionError
from baby.logging import get_logger
from baby.providers.base import ModelProvider
from baby.providers.types import (
    ChatMessage,
    ProviderResponse,
    TokenUsage,
    ToolDefinition,
)

logger = get_logger(__name__)

# Default models that OpenAI-compatible endpoints commonly support
_DEFAULT_MODELS = ["gpt-4", "gpt-4-turbo", "gpt-3.5-turbo"]


class OpenAIProvider(ModelProvider):
    """Model provider for OpenAI-compatible chat completion endpoints.

    Args:
        api_key: API key. Falls back to ``settings.openai_api_key``.
        base_url: Base URL for the API. Falls back to ``settings.openai_base_url``
                  if it exists, otherwise uses the openai SDK default.
        default_model: Default model identifier. Falls back to
                       ``settings.default_model``.
        supported_models: Explicit list of available model IDs. If None a
                          sensible default list is used.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_model: Optional[str] = None,
        supported_models: Optional[List[str]] = None,
    ) -> None:
        self._api_key = api_key or settings.openai_api_key
        self._base_url = base_url or getattr(settings, "openai_base_url", None)
        self._default_model = default_model or settings.default_model
        self._supported_models = supported_models or _DEFAULT_MODELS
        self._client = None  # Lazy-initialised

    # -- ModelProvider interface ------------------------------------------------

    @property
    def name(self) -> str:
        return "openai"

    @property
    def supported_models(self) -> List[str]:
        return list(self._supported_models)

    def is_available(self) -> bool:
        return self._api_key is not None and len(self._api_key) > 0

    # -- Completions -----------------------------------------------------------

    async def complete(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        stop: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> ProviderResponse:
        client = self._get_client()
        resolved_model = model or self._default_model

        params: Dict[str, Any] = {
            "model": resolved_model,
            "messages": [self._to_api_message(m) for m in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if stop is not None:
            params["stop"] = stop
        params.update(kwargs)

        try:
            response = await client.chat.completions.create(**params)
            return self._parse_response(response, resolved_model)
        except Exception as exc:
            logger.error("OpenAI provider call failed", model=resolved_model, error=str(exc))
            raise ExecutionError(f"OpenAI provider call failed: {exc}") from exc

    async def complete_with_tools(
        self,
        messages: List[ChatMessage],
        tools: List[ToolDefinition],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        **kwargs: Any,
    ) -> ProviderResponse:
        client = self._get_client()
        resolved_model = model or self._default_model

        openai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

        params: Dict[str, Any] = {
            "model": resolved_model,
            "messages": [self._to_api_message(m) for m in messages],
            "temperature": temperature,
            "tools": openai_tools,
        }
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        params.update(kwargs)

        try:
            response = await client.chat.completions.create(**params)
            return self._parse_response(response, resolved_model)
        except Exception as exc:
            logger.error("OpenAI provider tool call failed", model=resolved_model, error=str(exc))
            raise ExecutionError(f"OpenAI provider call failed: {exc}") from exc

    # -- Internal helpers ------------------------------------------------------

    def _get_client(self):
        """Lazily create the async OpenAI client."""
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise ExecutionError(
                    "openai package is required for OpenAIProvider. Install with: pip install openai>=1.0"
                ) from exc

            client_kwargs: Dict[str, Any] = {"api_key": self._api_key}
            if self._base_url:
                client_kwargs["base_url"] = self._base_url
            self._client = AsyncOpenAI(**client_kwargs)
        return self._client

    @staticmethod
    def _to_api_message(msg: ChatMessage) -> Dict[str, Any]:
        """Convert a ChatMessage to the dict format expected by the OpenAI SDK."""
        d: Dict[str, Any] = {"role": msg.role, "content": msg.content}
        if msg.name:
            d["name"] = msg.name
        if msg.tool_calls:
            d["tool_calls"] = msg.tool_calls
        if msg.tool_call_id:
            d["tool_call_id"] = msg.tool_call_id
        return d

    @staticmethod
    def _parse_response(response, resolved_model: str) -> ProviderResponse:
        """Parse an OpenAI SDK response into a ProviderResponse."""
        choice = response.choices[0] if response.choices else None
        content = ""
        finish_reason = None
        tool_calls = None

        if choice:
            content = choice.message.content or ""
            finish_reason = choice.finish_reason
            if choice.message.tool_calls:
                tool_calls = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in choice.message.tool_calls
                ]

        usage = TokenUsage()
        if response.usage:
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens or 0,
                completion_tokens=response.usage.completion_tokens or 0,
                total_tokens=response.usage.total_tokens or 0,
            )

        return ProviderResponse(
            content=content,
            model=response.model or resolved_model,
            usage=usage,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
        )
