"""NVIDIA model provider (API Catalog / NIM): selection, configuration and a real HTTP round trip.

The round-trip tests run the CodingAgent through the real ``openai`` SDK against a local
server that behaves like NVIDIA's OpenAI-compatible endpoint: it requires the ``nvapi-``
bearer key, the configured NVIDIA model and a strict tool-message schema, and answers with
genuine tool calls. No network access or NVIDIA account is needed.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List
from unittest.mock import patch

import pytest

from baby.agents.coding.agent import CodingAgent, create_default_coding_spec
from baby.configuration import Settings, settings, validate_settings
from baby.core import ExecutionContext, PermissionCategory, PermissionLevel, PlanStep, Task, ToolPermission
from baby.errors import ConfigurationError
from baby.observability.redaction import redact_sensitive_data
from baby.permissions import permission_manager
from baby.providers import NvidiaProvider, OpenAIProvider, provider_registry
from baby.providers.registry import (
    ProviderRegistry,
    active_provider_name,
    configured_model,
    register_configured_providers,
)
from baby.providers.types import ChatMessage
from baby.workers.process import FORWARDABLE_SETTINGS

NVIDIA_KEY = "nvapi-" + "A1b2C3d4E5f6G7h8I9j0" * 3
MODEL = "nvidia/nemotron-3-super-120b-a12b"


def _cfg(**overrides: Any) -> Settings:
    values: Dict[str, Any] = {
        "model_provider": "auto",
        "nvidia_api_key": None,
        "openai_api_key": None,
        "_env_file": None,
    }
    values.update(overrides)
    return Settings(**values)


# ---------------------------------------------------------------------------
# A local stand-in for https://integrate.api.nvidia.com/v1
# ---------------------------------------------------------------------------


class FakeNim:
    """Records requests and replays scripted chat-completion responses."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.responses: List[Dict[str, Any]] = []
        self.status = 200

    def handler(self) -> type:
        nim = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep test output quiet
                pass

            def _send(self, status: int, body: Dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                nim.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
                if self.path != "/v1/chat/completions":
                    return self._send(404, {"error": "not found"})
                if self.headers.get("Authorization") != f"Bearer {NVIDIA_KEY}" or nim.status == 401:
                    return self._send(401, {"status": 401, "title": "Unauthorized", "detail": "Invalid key"})
                if body.get("model") != MODEL:
                    return self._send(404, {"status": 404, "detail": f"Function '{body.get('model')}': Not found"})
                for message in body["messages"]:
                    if message["role"] == "tool" and set(message) - {"role", "content", "tool_call_id"}:
                        return self._send(400, {"detail": "Extra inputs are not permitted"})
                return self._send(200, nim.responses.pop(0))

        return Handler


def _completion(content: str = "", tool_calls: Any = None, tokens: int = 50) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if tool_calls else "stop"}],
        "usage": {"prompt_tokens": tokens - 10, "completion_tokens": 10, "total_tokens": tokens},
    }


def _tool_call(call_id: str, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


@pytest.fixture
def fake_nim() -> Iterator[tuple]:
    nim = FakeNim()
    server = ThreadingHTTPServer(("127.0.0.1", 0), nim.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield nim, f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "main.py").write_text("print('baby')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def read_permission() -> Iterator[None]:
    permission_manager.clear()
    permission_manager.grant_permission(
        "coding-agent",
        ToolPermission(category=PermissionCategory.LOCAL_READ, level=PermissionLevel.ALLOW, description="Read"),
    )
    permission_manager.grant_permission(
        "coding-agent",
        ToolPermission(
            category=PermissionCategory.LOCAL_WRITE, level=PermissionLevel.APPROVAL_REQUIRED, description="Write"
        ),
    )
    yield
    permission_manager.clear()
    provider_registry.clear()


def _context(agent: CodingAgent, description: str) -> ExecutionContext:
    task = Task(title="NVIDIA task", description=description)
    return ExecutionContext(
        task_id=task.id, plan_step=PlanStep(step_id=1, description=description), task=task, agent=agent.spec
    )


# ---------------------------------------------------------------------------
# Provider selection and configuration
# ---------------------------------------------------------------------------


class TestSelection:
    @pytest.mark.parametrize(
        "choice, nvidia, openai, expected",
        [
            ("auto", None, None, None),
            ("auto", NVIDIA_KEY, None, "nvidia"),
            ("auto", None, "sk-x", "openai"),
            ("auto", NVIDIA_KEY, "sk-x", "nvidia"),
            ("openai", NVIDIA_KEY, "sk-x", "openai"),
            ("nvidia", None, "sk-x", None),
            ("openai", NVIDIA_KEY, None, None),
            ("NVIDIA", NVIDIA_KEY, None, "nvidia"),
            ("auto", "   ", None, None),
        ],
    )
    def test_active_provider(self, choice, nvidia, openai, expected):
        assert active_provider_name(_cfg(model_provider=choice, nvidia_api_key=nvidia, openai_api_key=openai)) == (
            expected
        )

    def test_configured_model_follows_active_provider(self):
        assert configured_model(_cfg(nvidia_api_key=NVIDIA_KEY, nvidia_model=MODEL)) == MODEL
        assert configured_model(_cfg(openai_api_key="sk-x", default_model="gpt-4o")) == "gpt-4o"
        assert configured_model(_cfg(default_model="gpt-4o")) == "gpt-4o"

    def test_coding_agent_spec_uses_nvidia_model(self):
        with patch.multiple(settings, model_provider="auto", nvidia_api_key=NVIDIA_KEY, nvidia_model=MODEL):
            assert create_default_coding_spec().model == MODEL

    def test_register_configured_nvidia_provider(self):
        registry = ProviderRegistry()
        with patch.multiple(settings, model_provider="auto", nvidia_api_key=NVIDIA_KEY, openai_api_key=None):
            assert register_configured_providers(registry) == ["nvidia"]
            assert register_configured_providers(registry) == ["nvidia"]  # idempotent
        provider = registry.get_default()
        assert isinstance(provider, NvidiaProvider)
        assert provider.is_available()

    def test_nothing_registered_without_keys(self):
        registry = ProviderRegistry()
        with patch.multiple(settings, model_provider="auto", nvidia_api_key=None, openai_api_key=None):
            assert register_configured_providers(registry) == []
        assert registry.list_providers() == []

    def test_explicit_openai_choice_registers_openai(self):
        registry = ProviderRegistry()
        with patch.multiple(settings, model_provider="openai", nvidia_api_key=NVIDIA_KEY, openai_api_key="sk-x"):
            assert register_configured_providers(registry) == ["openai"]
        assert type(registry.get_default()) is OpenAIProvider

    def test_provider_defaults(self):
        with patch.multiple(
            settings,
            nvidia_api_key=NVIDIA_KEY,
            nvidia_base_url="https://integrate.api.nvidia.com/v1",
            nvidia_model=MODEL,
        ):
            provider = NvidiaProvider()
        assert provider.name == "nvidia"
        assert provider.supported_models == [MODEL]
        assert provider._base_url == "https://integrate.api.nvidia.com/v1"
        assert NvidiaProvider(api_key="   ").is_available() is False

    def test_shipped_defaults_point_at_nvidia_catalog(self):
        cfg = _cfg()
        assert cfg.nvidia_base_url == "https://integrate.api.nvidia.com/v1"
        assert cfg.nvidia_model == MODEL
        assert cfg.model_provider == "auto"

    def test_non_secret_provider_settings_reach_workers(self):
        assert {"model_provider", "nvidia_base_url", "nvidia_model", "default_model"} <= FORWARDABLE_SETTINGS
        assert "nvidia_api_key" not in FORWARDABLE_SETTINGS


class TestValidation:
    def test_unknown_provider_fails_closed(self):
        with pytest.raises(ConfigurationError, match="model_provider"):
            validate_settings(_cfg(model_provider="nvidiaa"))

    def test_chosen_provider_without_key_warns(self):
        assert any("NVIDIA key" in w for w in validate_settings(_cfg(model_provider="nvidia")))
        assert any("OpenAI key" in w for w in validate_settings(_cfg(model_provider="openai")))

    def test_nvidia_requires_model(self):
        with pytest.raises(ConfigurationError, match="nvidia_model"):
            validate_settings(_cfg(model_provider="nvidia", nvidia_api_key=NVIDIA_KEY, nvidia_model=" "))

    def test_auto_without_keys_is_valid(self):
        assert not any("key is configured" in w for w in validate_settings(_cfg()))


class TestRedaction:
    def test_nvidia_key_scrubbed_from_strings_and_keys(self):
        data = {"error": f"failed with {NVIDIA_KEY}", "nvidia_api_key": NVIDIA_KEY}
        result = redact_sensitive_data(data)
        assert NVIDIA_KEY not in json.dumps(result)
        assert result["nvidia_api_key"] == "[REDACTED]"

    def test_tool_message_omits_name(self):
        message = ChatMessage(role="tool", content="{}", name="list_files", tool_call_id="call_1")
        assert OpenAIProvider._to_api_message(message) == {"role": "tool", "content": "{}", "tool_call_id": "call_1"}
        user = OpenAIProvider._to_api_message(ChatMessage(role="user", content="hi", name="alice"))
        assert user["name"] == "alice"


# ---------------------------------------------------------------------------
# Real HTTP round trips through the openai SDK
# ---------------------------------------------------------------------------


class TestRoundTrip:
    async def test_coding_agent_completes_task_with_nvidia_tool_calls(self, fake_nim, repo):
        nim, base_url = fake_nim
        nim.responses = [
            _completion(tool_calls=[_tool_call("call_1", "list_files", {"path": "."})], tokens=60),
            _completion(tool_calls=[_tool_call("call_2", "read_file", {"path": "main.py"})], tokens=80),
            _completion(content="main.py prints 'baby'.", tokens=100),
        ]
        provider = NvidiaProvider(api_key=NVIDIA_KEY, base_url=base_url, default_model=MODEL)
        spec = create_default_coding_spec()
        spec.model = MODEL
        agent = CodingAgent(spec=spec, provider=provider, repo_root=str(repo))

        result = await agent.execute(_context(agent, "Explain what main.py does"))

        assert result.success is True, result.error
        assert result.output["response"] == "main.py prints 'baby'."
        assert [r["tool"] for r in result.output["tool_results"]] == ["list_files", "read_file"]
        assert result.output["tool_results"][1]["result"]["content"] == "print('baby')\n"
        assert result.tokens_used == 240

        assert len(nim.requests) == 3
        first = nim.requests[0]["body"]
        assert first["model"] == MODEL
        assert {t["function"]["name"] for t in first["tools"]} == {"list_files", "read_file", "write_file"}
        # The tool result is fed back with the matching call id and NVIDIA-strict schema.
        tool_messages = [m for m in nim.requests[2]["body"]["messages"] if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_messages] == ["call_1", "call_2"]

    async def test_write_requested_by_model_still_needs_approval(self, fake_nim, repo):
        nim, base_url = fake_nim
        nim.responses = [
            _completion(tool_calls=[_tool_call("call_1", "write_file", {"path": "new.py", "content": "x = 1"})])
        ]
        provider = NvidiaProvider(api_key=NVIDIA_KEY, base_url=base_url, default_model=MODEL)
        agent = CodingAgent(provider=provider, repo_root=str(repo))
        agent.spec.model = MODEL

        result = await agent.execute(_context(agent, "Create new.py"))

        assert result.success is False
        assert "approval" in (result.error or "").lower()
        assert not (repo / "new.py").exists()

    async def test_rejected_key_fails_the_step_without_leaking_it(self, fake_nim, repo):
        nim, base_url = fake_nim
        nim.status = 401
        provider = NvidiaProvider(api_key=NVIDIA_KEY, base_url=base_url, default_model=MODEL)
        agent = CodingAgent(provider=provider, repo_root=str(repo))
        agent.spec.model = MODEL

        result = await agent.execute(_context(agent, "Explain main.py"))

        assert result.success is False
        assert "nvidia provider call failed" in (result.error or "")
        assert NVIDIA_KEY not in (result.error or "")

    async def test_unknown_model_fails_the_step(self, fake_nim, repo):
        _, base_url = fake_nim
        provider = NvidiaProvider(api_key=NVIDIA_KEY, base_url=base_url, default_model="gpt-4")
        agent = CodingAgent(provider=provider, repo_root=str(repo))
        agent.spec.model = "gpt-4"

        result = await agent.execute(_context(agent, "Explain main.py"))

        assert result.success is False
        assert "Not found" in (result.error or "")
