"""Tests for the model provider subsystem."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from baby.errors import ConfigurationError, ExecutionError
from baby.providers.base import ModelProvider
from baby.providers.openai_provider import OpenAIProvider
from baby.providers.registry import provider_registry
from baby.providers.types import (
    ChatMessage,
    ProviderResponse,
    TokenUsage,
    ToolDefinition,
)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class TestChatMessage:
    """Tests for ChatMessage."""

    def test_create_simple_message(self) -> None:
        msg = ChatMessage(role="user", content="Hello")
        assert msg.role == "user"
        assert msg.content == "Hello"
        assert msg.tool_calls is None

    def test_create_tool_call_message(self) -> None:
        msg = ChatMessage(
            role="assistant",
            content="",
            tool_calls=[{"id": "tc_1", "type": "function", "function": {"name": "f"}}],
        )
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1

    def test_role_required(self) -> None:
        with pytest.raises(Exception):
            ChatMessage(role="", content="hi")


class TestTokenUsage:
    """Tests for TokenUsage."""

    def test_defaults_to_zero(self) -> None:
        usage = TokenUsage()
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.total_tokens == 0

    def test_explicit_values(self) -> None:
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        assert usage.total_tokens == 150


class TestProviderResponse:
    """Tests for ProviderResponse."""

    def test_default_response(self) -> None:
        resp = ProviderResponse()
        assert resp.content == ""
        assert resp.model == ""
        assert resp.usage.total_tokens == 0
        assert resp.tool_calls is None

    def test_full_response(self) -> None:
        resp = ProviderResponse(
            content="Answer",
            model="gpt-4",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            finish_reason="stop",
        )
        assert resp.content == "Answer"
        assert resp.finish_reason == "stop"


class TestToolDefinition:
    """Tests for ToolDefinition."""

    def test_create_tool_def(self) -> None:
        td = ToolDefinition(
            name="search",
            description="Search the web",
            parameters={"type": "object", "properties": {"q": {"type": "string"}}},
        )
        assert td.name == "search"

    def test_name_required(self) -> None:
        with pytest.raises(Exception):
            ToolDefinition(name="", description="d")


# ---------------------------------------------------------------------------
# Abstract interface compliance
# ---------------------------------------------------------------------------


class _StubProvider(ModelProvider):
    """Minimal concrete provider for interface testing."""

    @property
    def name(self) -> str:
        return "stub"

    @property
    def supported_models(self):
        return ["stub-model"]

    def is_available(self) -> bool:
        return True

    async def complete(self, messages, **kwargs):
        return ProviderResponse(content="stub", model="stub-model")

    async def complete_with_tools(self, messages, tools, **kwargs):
        return ProviderResponse(content="stub-tools", model="stub-model")


class TestModelProviderInterface:
    """Tests for the abstract ModelProvider interface."""

    def test_stub_implements_interface(self) -> None:
        provider = _StubProvider()
        assert provider.name == "stub"
        assert provider.is_available() is True
        assert "stub-model" in provider.supported_models

    @pytest.mark.asyncio
    async def test_stub_complete(self) -> None:
        provider = _StubProvider()
        msgs = [ChatMessage(role="user", content="hi")]
        resp = await provider.complete(msgs)
        assert resp.content == "stub"

    @pytest.mark.asyncio
    async def test_stub_complete_with_tools(self) -> None:
        provider = _StubProvider()
        msgs = [ChatMessage(role="user", content="search")]
        tools = [ToolDefinition(name="search", description="search")]
        resp = await provider.complete_with_tools(msgs, tools)
        assert resp.content == "stub-tools"

    def test_get_info(self) -> None:
        provider = _StubProvider()
        info = provider.get_info()
        assert info["name"] == "stub"
        assert info["available"] is True


# ---------------------------------------------------------------------------
# Provider Registry
# ---------------------------------------------------------------------------


class TestProviderRegistry:
    """Tests for ProviderRegistry."""

    def setup_method(self) -> None:
        provider_registry.clear()

    def test_register_and_get(self) -> None:
        p = _StubProvider()
        provider_registry.register(p)
        assert provider_registry.get("stub") is p

    def test_register_duplicate_raises(self) -> None:
        provider_registry.register(_StubProvider())
        with pytest.raises(ValueError, match="already registered"):
            provider_registry.register(_StubProvider())

    def test_get_nonexistent_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="not registered"):
            provider_registry.get("missing")

    def test_unregister(self) -> None:
        provider_registry.register(_StubProvider())
        provider_registry.unregister("stub")
        with pytest.raises(ConfigurationError):
            provider_registry.get("stub")

    def test_unregister_nonexistent_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="not registered"):
            provider_registry.unregister("nope")

    def test_default_provider(self) -> None:
        p = _StubProvider()
        provider_registry.register(p, default=True)
        assert provider_registry.get_default() is p

    def test_first_registered_becomes_default(self) -> None:
        p = _StubProvider()
        provider_registry.register(p)
        assert provider_registry.get_default() is p

    def test_no_default_raises(self) -> None:
        with pytest.raises(ConfigurationError, match="No providers"):
            provider_registry.get_default()

    def test_list_providers(self) -> None:
        provider_registry.register(_StubProvider())
        assert len(provider_registry.list_providers()) == 1

    def test_get_provider_for_model(self) -> None:
        provider_registry.register(_StubProvider())
        found = provider_registry.get_provider_for_model("stub-model")
        assert found is not None
        assert found.name == "stub"

    def test_get_provider_for_unknown_model(self) -> None:
        provider_registry.register(_StubProvider())
        assert provider_registry.get_provider_for_model("unknown-model") is None

    def test_clear(self) -> None:
        provider_registry.register(_StubProvider())
        provider_registry.clear()
        assert len(provider_registry.list_providers()) == 0


# ---------------------------------------------------------------------------
# OpenAI Provider (mocked — no real API calls)
# ---------------------------------------------------------------------------


class TestOpenAIProvider:
    """Tests for OpenAIProvider with mocked OpenAI SDK."""

    def test_is_available_with_key(self) -> None:
        provider = OpenAIProvider(api_key="sk-test-key")
        assert provider.is_available() is True

    def test_is_not_available_without_key(self) -> None:
        provider = OpenAIProvider(api_key="")
        assert provider.is_available() is False

    def test_name(self) -> None:
        provider = OpenAIProvider(api_key="sk-test")
        assert provider.name == "openai"

    def test_supported_models(self) -> None:
        provider = OpenAIProvider(api_key="sk-test", supported_models=["custom-model"])
        assert "custom-model" in provider.supported_models

    def test_custom_base_url(self) -> None:
        provider = OpenAIProvider(api_key="sk-test", base_url="http://localhost:8000/v1")
        assert provider._base_url == "http://localhost:8000/v1"

    def test_get_info(self) -> None:
        provider = OpenAIProvider(api_key="sk-test")
        info = provider.get_info()
        assert info["name"] == "openai"
        assert info["available"] is True

    @pytest.mark.asyncio
    async def test_complete_mocked(self) -> None:
        """Test complete() with a fully mocked OpenAI client."""
        provider = OpenAIProvider(api_key="sk-test")

        # Build mock response matching OpenAI SDK structure
        mock_message = MagicMock()
        mock_message.content = "Hello from the model"
        mock_message.tool_calls = None

        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "stop"

        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5
        mock_usage.total_tokens = 15

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = mock_usage
        mock_response.model = "gpt-4"

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        provider._client = mock_client

        messages = [ChatMessage(role="user", content="Hello")]
        resp = await provider.complete(messages)

        assert resp.content == "Hello from the model"
        assert resp.model == "gpt-4"
        assert resp.usage.total_tokens == 15
        assert resp.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_complete_with_tools_mocked(self) -> None:
        """Test complete_with_tools() with a fully mocked OpenAI client."""
        provider = OpenAIProvider(api_key="sk-test")

        mock_tc_fn = MagicMock()
        mock_tc_fn.name = "search"
        mock_tc_fn.arguments = '{"q": "test"}'

        mock_tc = MagicMock()
        mock_tc.id = "tc_1"
        mock_tc.type = "function"
        mock_tc.function = mock_tc_fn

        mock_message = MagicMock()
        mock_message.content = ""
        mock_message.tool_calls = [mock_tc]

        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_choice.finish_reason = "tool_calls"

        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.usage = None
        mock_response.model = "gpt-4"

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)
        provider._client = mock_client

        messages = [ChatMessage(role="user", content="search for X")]
        tools = [ToolDefinition(name="search", description="search", parameters={})]
        resp = await provider.complete_with_tools(messages, tools)

        assert resp.tool_calls is not None
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0]["function"]["name"] == "search"

    @pytest.mark.asyncio
    async def test_complete_api_error(self) -> None:
        """Provider wraps SDK exceptions in ExecutionError."""
        provider = OpenAIProvider(api_key="sk-test")

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("API down"))
        provider._client = mock_client

        messages = [ChatMessage(role="user", content="hi")]
        with pytest.raises(ExecutionError, match="API down"):
            await provider.complete(messages)

    @pytest.mark.asyncio
    async def test_complete_with_tools_api_error(self) -> None:
        """Provider wraps tool-call SDK exceptions in ExecutionError."""
        provider = OpenAIProvider(api_key="sk-test")

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("rate limit"))
        provider._client = mock_client

        messages = [ChatMessage(role="user", content="hi")]
        tools = [ToolDefinition(name="t", description="d")]
        with pytest.raises(ExecutionError, match="rate limit"):
            await provider.complete_with_tools(messages, tools)

    def test_missing_openai_package(self) -> None:
        """ExecutionError is raised if the openai package is not installed."""
        provider = OpenAIProvider(api_key="sk-test")
        provider._client = None  # Force re-init

        with patch.dict("sys.modules", {"openai": None}):
            with pytest.raises(ExecutionError, match="openai package is required"):
                provider._get_client()
