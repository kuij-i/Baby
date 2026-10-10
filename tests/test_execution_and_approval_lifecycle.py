"""Phase 17 / M2: truthful task execution.

Covers:
- D1: a model provider is optional. When OPENAI_API_KEY is configured the worker bootstrap
  registers the OpenAI-compatible provider; without one the CodingAgent runs explicit tool
  steps and "list files" requests and fails every other step instead of reporting success.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from baby.agents import agent_registry
from baby.agents.coding import CODING_AGENT_PERMISSIONS, CodingAgent, create_default_coding_spec
from baby.audit import audit_log
from baby.configuration import settings
from baby.core import (
    AuditEventType,
    ExecutionContext,
    PlanStep,
    Task,
    TaskStatus,
)
from baby.observability import task_tracker, worker_tracker
from baby.permissions import permission_manager
from baby.persistence.database import db_manager
from baby.persistence.sqlite_workers import SQLiteWorkerStore
from baby.providers.base import ModelProvider
from baby.providers.openai_provider import OpenAIProvider
from baby.providers.registry import provider_registry, register_configured_providers
from baby.providers.types import ProviderResponse, TokenUsage
from baby.workers.bootstrap import default_bootstrap
from baby.workers.queue import DurableTaskQueue
from baby.workers.runtime import WorkerRuntime, WorkerRuntimeConfig, new_worker_id

ENG = "eng-m2"
ADMIN_TOKEN = "admin-token-for-tests-0123456789abcdef"
OPERATOR_TOKEN = "op-token-for-tests-0123456789abcdef"


@pytest.fixture(autouse=True)
def durable_env(tmp_path: Path):
    """Isolated file-backed SQLite database and clean registries for every test."""
    original_url = db_manager.database_url
    db_manager.configure(f"sqlite:///{tmp_path / 'baby.db'}")
    db_manager.init_schema()
    for reset in (task_tracker.clear, audit_log.clear, agent_registry.clear, permission_manager.clear):
        reset()
    worker_tracker.clear()
    provider_registry.clear()
    yield tmp_path
    provider_registry.clear()
    audit_log.use_backend(None)
    audit_log.clear()
    agent_registry.clear()
    permission_manager.clear()
    db_manager.configure(original_url)
    db_manager.init_schema()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "hello.txt").write_text("hi\n", encoding="utf-8")
    return root


def _queue() -> DurableTaskQueue:
    assert task_tracker.store is not None
    return DurableTaskQueue(task_tracker.store, SQLiteWorkerStore(task_tracker.store.db), lease_duration_seconds=30)


def _submit(description: str, priority: int = 0) -> Any:
    return _queue().submit(Task(title="m2", description=description, priority=priority, user_id=ENG))


def _runtime() -> WorkerRuntime:
    config = WorkerRuntimeConfig(
        worker_id=new_worker_id(),
        poll_interval=0.01,
        heartbeat_interval=0.02,
        stale_timeout=10.0,
        lease_duration=10.0,
        max_task_seconds=30.0,
    )
    return WorkerRuntime(config)


async def _execute_next(runtime: WorkerRuntime) -> Optional[TaskStatus]:
    claimed = runtime.queue.claim_next(runtime.worker_id)
    assert claimed is not None
    return await runtime.execute_claimed(claimed)


def _register_coding_agent(repo: Path, provider: Optional[ModelProvider] = None) -> CodingAgent:
    agent = CodingAgent(provider=provider, repo_root=str(repo))
    agent_registry.register(agent)
    for perm in CODING_AGENT_PERMISSIONS:
        permission_manager.grant_permission("coding-agent", perm)
    return agent


def _context(description: str, tool_name: Optional[str] = None) -> ExecutionContext:
    task = Task(title="t", description=description, user_id=ENG)
    return ExecutionContext(
        task_id=task.id,
        plan_step=PlanStep(step_id=1, description=description, tool_name=tool_name),
        task=task,
        agent=create_default_coding_spec(),
    )


class _ScriptedProvider(ModelProvider):
    """Provider that answers immediately (no tool calls)."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def supported_models(self) -> List[str]:
        return ["gpt-4"]

    def is_available(self) -> bool:
        return True

    async def complete(self, messages: Any, **kwargs: Any) -> ProviderResponse:
        return ProviderResponse(content="done", model="gpt-4")

    async def complete_with_tools(self, messages: Any, tools: Any, **kwargs: Any) -> ProviderResponse:
        self.calls += 1
        return ProviderResponse(content="analysis complete", model="gpt-4")


class _ShiftingWriteProvider(_ScriptedProvider):
    """Proposes a different write on every call, so no approval ever matches a later run."""

    async def complete_with_tools(self, messages: Any, tools: Any, **kwargs: Any) -> ProviderResponse:
        self.calls += 1
        usage = TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        if messages and messages[-1].role == "tool":
            return ProviderResponse(content="written", model="gpt-4", usage=usage)
        call: Dict[str, Any] = {
            "id": f"call_{self.calls}",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": f"f{self.calls}.py", "content": "x = 1\n"}),
            },
        }
        return ProviderResponse(content="writing", model="gpt-4", tool_calls=[call], usage=usage)


# ============================================================================
# D1: truthful execution with an optional model provider
# ============================================================================


class TestTruthfulExecution:
    async def test_step_without_provider_fails_instead_of_reporting_success(self, repo):
        agent = _register_coding_agent(repo)
        result = await agent.execute(_context("Write a poem about cats"))
        assert result.success is False
        assert "No model provider is configured" in (result.error or "")
        assert "nothing was executed" in (result.error or "")
        types = [e.event_type for e in audit_log.get_events()]
        assert AuditEventType.TOOL_INVOKED not in types

    async def test_list_files_request_still_runs_without_provider(self, repo):
        agent = _register_coding_agent(repo)
        result = await agent.execute(_context("Please list files"))
        assert result.success is True
        assert [e["path"] for e in result.output["entries"]] == ["hello.txt"]  # type: ignore[index]

    @pytest.mark.parametrize("description", ["update the tools and details", "else", "falls back", "realistic"])
    def test_list_heuristic_matches_whole_words_only(self, repo, description):
        import asyncio

        agent = _register_coding_agent(repo)
        result = asyncio.run(agent.execute(_context(description)))
        assert result.success is False
        assert "No model provider is configured" in (result.error or "")

    async def test_explicit_tool_step_runs_without_provider(self, repo):
        agent = _register_coding_agent(repo)
        result = await agent.execute(_context('read {"path": "hello.txt"}', tool_name="read_file"))
        assert result.success is True
        assert result.output["content"] == "hi\n"  # type: ignore[index]

    async def test_available_provider_drives_the_model_loop(self, repo):
        provider = _ScriptedProvider()
        agent = _register_coding_agent(repo, provider=provider)
        result = await agent.execute(_context("Write a poem about cats"))
        assert result.success is True and provider.calls == 1

    async def test_worker_records_failed_not_completed_without_provider(self, repo):
        _register_coding_agent(repo)
        runtime = _runtime()
        runtime.startup()
        try:
            record = _submit("Write a poem about cats")
            assert await _execute_next(runtime) == TaskStatus.FAILED
        finally:
            runtime.shutdown()
        stored = task_tracker.get_task(record.task_id)
        assert stored.status == TaskStatus.FAILED  # type: ignore[union-attr]
        assert "No model provider is configured" in stored.error  # type: ignore[union-attr,operator]

    def test_coding_spec_model_follows_default_model_setting(self):
        with patch.object(settings, "default_model", "meta/llama-3.1-70b-instruct"):
            assert create_default_coding_spec().model == "meta/llama-3.1-70b-instruct"


class TestProviderRegistration:
    def test_nothing_registered_without_credentials(self):
        with patch.object(settings, "openai_api_key", None):
            assert register_configured_providers() == []
        assert provider_registry.list_providers() == []

    def test_whitespace_key_is_not_a_credential(self):
        with patch.object(settings, "openai_api_key", "   "):
            assert register_configured_providers() == []

    def test_openai_provider_registered_as_default_when_key_configured(self):
        with patch.object(settings, "openai_api_key", "sk-test-not-a-real-key-000000000000"):
            assert register_configured_providers() == ["openai"]
            assert register_configured_providers() == ["openai"]  # idempotent
        default = provider_registry.get_default()
        assert isinstance(default, OpenAIProvider) and default.is_available()
        assert len(provider_registry.list_providers()) == 1

    def test_default_bootstrap_registers_provider_only_with_key(self, repo):
        with patch.object(settings, "coding_repo_root", str(repo)):
            with patch.object(settings, "openai_api_key", None):
                default_bootstrap()
            assert provider_registry.list_providers() == []
            with patch.object(settings, "openai_api_key", "sk-test-not-a-real-key-000000000000"):
                default_bootstrap()
        assert [p.name for p in provider_registry.list_providers()] == ["openai"]
