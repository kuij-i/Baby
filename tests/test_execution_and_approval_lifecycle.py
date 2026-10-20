"""Phase 17 / M2: truthful task execution and approval lifecycle.

Covers:
- D1: a model provider is optional. When OPENAI_API_KEY is configured the worker bootstrap
  registers the OpenAI-compatible provider; without one the CodingAgent runs explicit tool
  steps and explicit file-listing requests ("ls [path]", "list files [in <path>]") and fails every other
  step instead of reporting success.
- Approval expiry (approval_ttl_seconds): undecided requests and unused grants expire; an
  expired request cannot be decided, an expired grant is never claimed or consumed, and
  recovery fails such tasks closed.
- Approval-round cap (max_approval_rounds): a task that keeps needing approval fails closed
  instead of pausing again.
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from baby.agents import agent_registry
from baby.agents.coding import CODING_AGENT_PERMISSIONS, CodingAgent, create_default_coding_spec
from baby.agents.coding.agent import _parse_list_request
from baby.api.app import app
from baby.audit import audit_log
from baby.configuration import Settings, settings, validate_settings
from baby.core import (
    AgentId,
    AuditEventType,
    ExecutionContext,
    PlanStep,
    Task,
    TaskStatus,
)
from baby.errors import ApprovalExpiredError, ConfigurationError, InvalidStateTransitionError
from baby.observability import task_tracker, worker_tracker
from baby.permissions import permission_manager
from baby.permissions.approvals import approval_service
from baby.persistence.database import db_manager
from baby.persistence.sqlite_workers import SQLiteWorkerStore
from baby.providers.base import ModelProvider
from baby.providers.openai_provider import OpenAIProvider
from baby.providers.registry import provider_registry, register_configured_providers
from baby.providers.types import ProviderResponse, TokenUsage
from baby.workers.bootstrap import default_bootstrap
from baby.workers.process import FORWARDABLE_SETTINGS
from baby.workers.queue import DurableTaskQueue, approvals_ready
from baby.workers.recovery import REASON_APPROVAL_EXPIRED, WorkerRecovery
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


# Explicit file-listing requests the CodingAgent may run without a model provider, with the list_files args.
LIST_REQUESTS: List[Any] = [
    ("ls", {}),
    ("ls src", {"path": "src"}),
    ("list files", {}),
    ("list the files", {}),
    ("list all files", {}),
    ("list all the files", {}),
    ("list files in src", {"path": "src"}),
    ("list files under src/sub", {"path": "src/sub"}),
    ("list directory", {}),
    ("list the directory", {}),
    ("list dir src", {"path": "src"}),
    ("please list files", {}),
    ("Please list files", {}),
    ("  List   Files.  ", {}),
    ("list files?", {}),
    ("list files in src.", {"path": "src"}),
    ("list files in .", {"path": "."}),
    ("list files in ../..", {"path": "../.."}),
    ("LS SRC/Sub", {"path": "SRC/Sub"}),
    ('list files {"path": "src"}', {"path": "src"}),
    ('list files {"path": "src", "recursive": false}', {"path": "src", "recursive": False}),
    ('list files in src {"recursive": false}', {"path": "src", "recursive": False}),
]

# Anything else must fail with "No model provider is configured" and run nothing.
NOT_LIST_REQUESTS: List[str] = [
    "update the tools and details",
    "else",
    "falls back",
    "realistic",
    "List the security bugs in auth.py",
    "list the bugs in auth.py",
    "list todos",
    "can you list what main.py does",
    "don't list files, explain X",
    "listing",
    "playlist",
    "ls -la; rm -rf /",
    "ls -la",
    "list files and explain them",
    "list files {not json}",
    'list files {"path": "src"} and more',
    "List repo",
]


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

    @pytest.mark.parametrize("description", NOT_LIST_REQUESTS)
    def test_non_listing_requests_fail_without_provider(self, repo, description):
        import asyncio

        agent = _register_coding_agent(repo)
        result = asyncio.run(agent.execute(_context(description)))
        assert result.success is False
        assert "No model provider is configured" in (result.error or "")
        assert AuditEventType.TOOL_INVOKED not in [e.event_type for e in audit_log.get_events()]

    @pytest.mark.parametrize(("description", "expected"), LIST_REQUESTS)
    def test_parse_list_request_accepts_explicit_listing(self, description, expected):
        assert _parse_list_request(description) == expected

    @pytest.mark.parametrize("description", NOT_LIST_REQUESTS + ['list files in a {"path": "b"}', "list files in"])
    def test_parse_list_request_rejects_everything_else(self, description):
        assert _parse_list_request(description) is None

    async def test_plain_text_path_goes_through_the_executor(self, repo):
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
        agent = _register_coding_agent(repo)
        result = await agent.execute(_context("list files in src"))
        assert result.success is True
        assert [e["path"] for e in result.output["entries"]] == ["src/a.py"]  # type: ignore[index]
        invoked = [e for e in audit_log.get_events() if e.event_type == AuditEventType.TOOL_INVOKED]
        assert [(e.details["tool"], e.details["input_keys"]) for e in invoked] == [("list_files", ["path"])]

    @pytest.mark.parametrize(
        ("description", "error_fragment"),
        [
            ("list files in ../../etc", "outside the configured repository root"),
            ("ls ..", "outside the configured repository root"),
            ("list files in .git", ".git"),
            ("list files in missing", "missing"),
        ],
    )
    async def test_listing_outside_the_repository_fails_in_the_executor(self, repo, description, error_fragment):
        agent = _register_coding_agent(repo)
        result = await agent.execute(_context(description))
        assert result.success is False
        assert error_fragment in (result.error or "").lower()
        # The parser accepted it; the executor's boundary checks refused it.
        assert AuditEventType.TOOL_INVOKED in [e.event_type for e in audit_log.get_events()]

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


# ============================================================================
# Approval expiry
# ============================================================================


def _park_with_request(priority: int = 9) -> Any:
    """Submit a high-priority task, run it once so it parks with one plan-step request."""
    from tests.worker_helpers import bootstrap_scripted_agent

    bootstrap_scripted_agent()
    return _submit("privileged", priority=priority)


class TestApprovalExpiry:
    @pytest.fixture(autouse=True)
    def _short_ttl(self):
        with patch.object(settings, "approval_ttl_seconds", 60.0):
            yield

    async def _parked(self) -> Any:
        record = _park_with_request()
        runtime = _runtime()
        runtime.startup()
        try:
            assert await _execute_next(runtime) == TaskStatus.AWAITING_APPROVAL
        finally:
            runtime.shutdown()
        stored = task_tracker.get_task(record.task_id)
        return stored, stored.approvals[0]  # type: ignore[union-attr]

    async def test_decision_within_ttl_is_recorded(self):
        record, request = await self._parked()
        decided = approval_service.decide(
            record.task_id, request.request_id, True, "admin-user", now=request.created_at + timedelta(seconds=59)
        )
        assert decided.status == TaskStatus.AWAITING_APPROVAL and decided.approvals[0].approved is True

    async def test_expired_request_cannot_be_decided_and_fails_task(self):
        record, request = await self._parked()
        with pytest.raises(ApprovalExpiredError, match="expired without a decision"):
            approval_service.decide(
                record.task_id, request.request_id, True, "admin-user", now=request.created_at + timedelta(minutes=2)
            )
        stored = task_tracker.get_task(record.task_id)
        assert stored.status == TaskStatus.FAILED  # type: ignore[union-attr]
        assert stored.approvals[0].approved is None  # no decision recorded  # type: ignore[union-attr]
        assert "expired" in stored.error  # type: ignore[union-attr,operator]

    async def test_recovery_fails_expired_undecided_request(self):
        record, request = await self._parked()
        recovery = WorkerRecovery()
        assert recovery.recover(now=request.created_at + timedelta(seconds=30)).expired_approval_tasks == []
        report = recovery.recover(now=request.created_at + timedelta(minutes=2))
        assert [r.task_id for r in report.expired_approval_tasks] == [record.task_id]
        assert task_tracker.get_task(record.task_id).status == TaskStatus.FAILED  # type: ignore[union-attr]
        assert recovery.recover(now=request.created_at + timedelta(minutes=3)).changed is False  # idempotent
        events = audit_log.get_events(task_id=record.task_id, event_type=AuditEventType.TASK_FAILED)
        assert any(e.details.get("reason") == REASON_APPROVAL_EXPIRED for e in events)

    async def test_unused_grant_expires_and_is_never_claimed(self):
        record, request = await self._parked()
        granted_at = request.created_at + timedelta(seconds=10)
        approval_service.decide(record.task_id, request.request_id, True, "admin-user", now=granted_at)
        stored = task_tracker.get_task(record.task_id)
        assert approvals_ready(stored, now=granted_at + timedelta(seconds=30)) is True  # type: ignore[arg-type]
        assert approvals_ready(stored, now=granted_at + timedelta(minutes=2)) is False  # type: ignore[arg-type]

        runtime = _runtime()
        runtime.startup()
        try:
            assert runtime.queue.claim_next(runtime.worker_id, now=granted_at + timedelta(minutes=2)) is None
        finally:
            runtime.shutdown()
        report = WorkerRecovery().recover(now=granted_at + timedelta(minutes=2))
        assert [r.task_id for r in report.expired_approval_tasks] == [record.task_id]
        failed = task_tracker.get_task(record.task_id)
        assert failed.status == TaskStatus.FAILED and "was not used" in failed.error  # type: ignore[union-attr,operator]

    async def test_expired_grant_is_never_consumed(self):
        record, request = await self._parked()
        granted_at = request.created_at + timedelta(seconds=10)
        approval_service.decide(record.task_id, request.request_id, True, "admin-user", now=granted_at)
        runtime = _runtime()
        runtime.startup()
        try:  # resume the task the normal way, inside the TTL
            assert runtime.queue.claim_next(runtime.worker_id, now=granted_at + timedelta(seconds=20)) is not None
        finally:
            runtime.shutdown()
        args = ("plan_step:1", "plan_step_approval", {"description": "privileged", "priority": 9}, "r")
        late = granted_at + timedelta(minutes=2)
        orchestrator = AgentId(id="orchestrator")
        assert not approval_service.authorize_or_request(record.task_id, orchestrator, ENG, *args, now=late)
        assert task_tracker.get_task(record.task_id).approvals[0].consumed_at is None  # type: ignore[union-attr]
        on_time = granted_at + timedelta(seconds=30)
        assert approval_service.authorize_or_request(
            record.task_id, AgentId(id="orchestrator"), ENG, *args, now=on_time
        )

    async def test_api_returns_409_for_expired_request(self):
        record, request = await self._parked()
        with patch.multiple(
            settings, api_require_auth=True, api_admin_token=ADMIN_TOKEN, api_auth_token=OPERATOR_TOKEN
        ):
            with patch("baby.permissions.approvals.utc_now") as fake_now:
                fake_now.return_value = request.created_at + timedelta(minutes=5)
                resp = TestClient(app, raise_server_exceptions=False).post(
                    f"/api/v1/tasks/{record.task_id}/approvals/{request.request_id}",
                    json={"approved": True},
                    headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
                )
        assert resp.status_code == 409 and "expired" in resp.json()["detail"]
        assert task_tracker.get_task(record.task_id).status == TaskStatus.FAILED  # type: ignore[union-attr]
        events = audit_log.get_events(task_id=record.task_id, event_type=AuditEventType.TASK_FAILED)
        assert any(e.details.get("reason") == "approval_expired" for e in events)


class TestCompletedAfterApproval:
    async def test_completed_task_carries_no_stale_approval_error(self):
        record = _park_with_request()
        runtime = _runtime()
        runtime.startup()
        try:
            assert await _execute_next(runtime) == TaskStatus.AWAITING_APPROVAL
            parked = task_tracker.get_task(record.task_id)
            assert parked.error == "Approval required for plan step 1"  # type: ignore[union-attr]
            approval_service.decide(record.task_id, parked.approvals[0].request_id, True, "admin-user")  # type: ignore
            assert await _execute_next(runtime) == TaskStatus.COMPLETED
        finally:
            runtime.shutdown()
        done = task_tracker.get_task(record.task_id)
        assert done.status == TaskStatus.COMPLETED and done.error is None  # type: ignore[union-attr]


# ============================================================================
# Approval-round cap
# ============================================================================


class TestApprovalRoundCap:
    async def test_non_converging_approval_loop_fails_closed_after_cap(self, repo):
        _register_coding_agent(repo, provider=_ShiftingWriteProvider())
        runtime = _runtime()
        runtime.startup()
        try:
            record = _submit("keep writing")
            with patch.object(settings, "max_approval_rounds", 3):
                for round_number in (1, 2, 3):
                    assert await _execute_next(runtime) == TaskStatus.AWAITING_APPROVAL, round_number
                    pending = task_tracker.get_task(record.task_id).approvals[-1]  # type: ignore[union-attr]
                    approval_service.decide(record.task_id, pending.request_id, True, "admin-user")
                assert await _execute_next(runtime) == TaskStatus.FAILED
        finally:
            runtime.shutdown()
        stored = task_tracker.get_task(record.task_id)
        assert stored.status == TaskStatus.FAILED  # type: ignore[union-attr]
        assert "max_approval_rounds (3)" in stored.error  # type: ignore[union-attr,operator]
        assert len(stored.approvals) == 4  # type: ignore[union-attr]
        with pytest.raises(InvalidStateTransitionError):
            approval_service.decide(record.task_id, stored.approvals[-1].request_id, True, "admin-user")  # type: ignore
        assert sorted(p.name for p in repo.iterdir()) == ["hello.txt"]  # no write ever ran unapproved

    async def test_tasks_within_the_cap_still_pause(self, repo):
        _register_coding_agent(repo, provider=_ShiftingWriteProvider())
        runtime = _runtime()
        runtime.startup()
        try:
            record = _submit("keep writing")
            with patch.object(settings, "max_approval_rounds", 1):
                assert await _execute_next(runtime) == TaskStatus.AWAITING_APPROVAL
                pending = task_tracker.get_task(record.task_id).approvals[-1]  # type: ignore[union-attr]
                approval_service.decide(record.task_id, pending.request_id, True, "admin-user")
                assert await _execute_next(runtime) == TaskStatus.FAILED
        finally:
            runtime.shutdown()
        assert "max_approval_rounds (1)" in task_tracker.get_task(record.task_id).error  # type: ignore[union-attr,operator]


# ============================================================================
# Configuration
# ============================================================================


class TestApprovalSettings:
    def test_defaults(self):
        fresh = Settings()
        assert fresh.approval_ttl_seconds == 86400.0 and fresh.max_approval_rounds == 3
        validate_settings(fresh)

    @pytest.mark.parametrize("value", [0, 59, 2_592_001, True, "x"])
    def test_ttl_bounds(self, value):
        cfg = Settings()
        cfg.approval_ttl_seconds = value  # type: ignore[assignment]
        with pytest.raises(ConfigurationError, match="approval_ttl_seconds"):
            validate_settings(cfg)

    @pytest.mark.parametrize("value", [0, 21, 2.5, True])
    def test_round_bounds(self, value):
        cfg = Settings()
        cfg.max_approval_rounds = value  # type: ignore[assignment]
        with pytest.raises(ConfigurationError, match="max_approval_rounds"):
            validate_settings(cfg)

    def test_settings_forwarded_to_worker_processes(self):
        assert {"approval_ttl_seconds", "max_approval_rounds"} <= FORWARDABLE_SETTINGS
        assert "openai_api_key" not in FORWARDABLE_SETTINGS  # secrets come from the environment
