"""Phase 18 F3: pluggable, persisted planning with plan-level approval.

Covers:
- RuleBasedPlanner (default) keeps the single-step behaviour; ModelPlanner validates strict JSON
  and fails closed on malformed output, too many steps, provider errors or a missing provider.
- A task is planned once: the plan is stored and reused when the task resumes after approval, so
  the planner is called exactly once and the plan approval digest matches across the resume.
- High-priority plans need one admin approval for the whole ordered step list before any step runs.
- Multi-step plans run in order and stop at the first failed step, naming it.
"""

import importlib
import json
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import patch

import pytest

from baby.agents import agent_registry
from baby.agents.base import Agent
from baby.agents.coding import CODING_AGENT_PERMISSIONS, CodingAgent
from baby.audit import audit_log
from baby.configuration import Settings, settings, validate_settings
from baby.core import (
    AgentCapability,
    AgentId,
    AgentResult,
    AgentSpec,
    AuditEventType,
    ExecutionContext,
    Plan,
    PlanStep,
    Task,
    TaskStatus,
)
from baby.errors import ConfigurationError, PlanningError
from baby.observability import task_tracker, worker_tracker
from baby.orchestration.orchestrator import (
    PLAN_APPROVAL_OPERATION,
    PLAN_APPROVAL_PERMISSION,
    orchestrator,
    plan_approval_arguments,
)
from baby.permissions import permission_manager
from baby.permissions.approvals import approval_service, operation_digest
from baby.persistence.database import db_manager
from baby.persistence.sqlite_workers import SQLiteWorkerStore
from baby.planning.planner import ModelPlanner, RuleBasedPlanner, TaskPlanner, parse_model_plan
from baby.providers.base import ModelProvider
from baby.providers.registry import provider_registry
from baby.providers.types import ProviderResponse
from baby.workers.process import FORWARDABLE_SETTINGS
from baby.workers.queue import DurableTaskQueue
from baby.workers.runtime import WorkerRuntime, WorkerRuntimeConfig, new_worker_id
from tests.worker_helpers import WriteFileProvider

ENG = "eng-planner"


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


class PlanProvider(ModelProvider):
    """Fake provider returning a fixed planner response (or raising) and counting calls."""

    def __init__(self, content: str = "", error: Optional[Exception] = None, available: bool = True) -> None:
        self.content = content
        self.error = error
        self.available = available
        self.calls = 0

    @property
    def name(self) -> str:
        return "plan-fake"

    @property
    def supported_models(self) -> List[str]:
        return ["gpt-4"]

    def is_available(self) -> bool:
        return self.available

    async def complete(self, messages: Any, **kwargs: Any) -> ProviderResponse:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ProviderResponse(content=self.content, model="gpt-4")

    async def complete_with_tools(self, messages: Any, tools: Any, **kwargs: Any) -> ProviderResponse:
        raise AssertionError("the planner never calls tools")


class StepAgent(Agent):
    """Records each step description it runs; a step described as ``fail`` fails."""

    def __init__(self) -> None:
        super().__init__(
            AgentSpec(
                id=AgentId(id="step-agent"),
                name="StepAgent",
                description="Records executed steps",
                role="testing",
                capabilities=[AgentCapability(name="test", description="test")],
            )
        )
        self.ran: List[str] = []
        self.previous_seen: List[List[int]] = []

    async def execute(self, context: ExecutionContext) -> AgentResult:
        self.ran.append(context.plan_step.description)
        self.previous_seen.append(sorted(context.previous_results))
        success = context.plan_step.description != "fail"
        return AgentResult(
            step_id=context.plan_step.step_id,
            agent_id=self.spec.id,
            success=success,
            error=None if success else "boom",
        )


def _steps_json(*descriptions: str) -> str:
    return json.dumps({"steps": [{"description": d} for d in descriptions]})


def _model_planner(provider: PlanProvider) -> Any:
    """Patch the orchestrator to plan with ``provider`` in model mode."""
    planner = TaskPlanner(model_planner=ModelPlanner(provider=provider))
    # Patch the module object, not a dotted path: ``baby.orchestration`` re-exports the
    # ``orchestrator`` instance under the submodule's name, and Python 3.10's mock resolves
    # dotted paths with getattr, so the string form would target the instance.
    return patch.object(importlib.import_module("baby.orchestration.orchestrator"), "task_planner", planner)


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


def _submit(description: str = "task", priority: int = 0) -> Any:
    assert task_tracker.store is not None
    queue = DurableTaskQueue(task_tracker.store, SQLiteWorkerStore(task_tracker.store.db), lease_duration_seconds=30)
    return queue.submit(Task(title="planner", description=description, priority=priority, user_id=ENG))


async def _execute_next(runtime: WorkerRuntime) -> Optional[TaskStatus]:
    claimed = runtime.queue.claim_next(runtime.worker_id)
    assert claimed is not None
    return await runtime.execute_claimed(claimed)


def _approve_latest(task_id: Any) -> None:
    pending = task_tracker.get_task(task_id).approvals[-1]  # type: ignore[union-attr]
    approval_service.decide(task_id, pending.request_id, True, "admin-user")


def _audit(event_type: AuditEventType) -> List[Any]:
    return [e for e in audit_log.get_events() if e.event_type == event_type]


# ============================================================================
# Planners
# ============================================================================


class TestRuleBasedPlanner:
    @pytest.mark.parametrize(("priority", "approval"), [(0, False), (7, False), (8, True), (10, True)])
    async def test_single_step_with_plan_level_approval(self, priority, approval):
        task = Task(title="t", description="do the thing", priority=priority)
        plan = await RuleBasedPlanner().plan(task)
        assert [(s.step_id, s.description) for s in plan.steps] == [(1, "do the thing")]
        assert plan.approval_required is approval and plan.planner_mode == "rule"
        assert not any(s.approval_required for s in plan.steps)

    async def test_task_planner_defaults_to_rule_mode(self):
        provider = PlanProvider(_steps_json("a", "b"))
        planner = TaskPlanner(model_planner=ModelPlanner(provider=provider))
        plan = await planner.plan(Task(title="t", description="x"))
        assert plan.planner_mode == "rule" and len(plan.steps) == 1 and provider.calls == 0

    async def test_task_planner_rejects_unknown_mode(self):
        with patch.object(settings, "planner_mode", "magic"):
            with pytest.raises(PlanningError, match="planner_mode"):
                await TaskPlanner().plan(Task(title="t", description="x"))


class TestModelPlanner:
    async def test_valid_json_becomes_ordered_steps(self):
        provider = PlanProvider(_steps_json("read main.py", "  summarise it  "))
        with patch.object(settings, "planner_mode", "model"):
            plan = await TaskPlanner(model_planner=ModelPlanner(provider=provider)).plan(
                Task(title="t", description="x", priority=9)
            )
        assert [(s.step_id, s.description) for s in plan.steps] == [(1, "read main.py"), (2, "summarise it")]
        assert plan.planner_mode == "model" and plan.approval_required is True and provider.calls == 1

    async def test_single_json_fence_is_accepted(self):
        provider = PlanProvider("```json\n" + _steps_json("one") + "\n```")
        plan = await ModelPlanner(provider=provider, max_steps=5).plan(Task(title="t", description="x"))
        assert [s.description for s in plan.steps] == ["one"]

    @pytest.mark.parametrize(
        ("content", "match"),
        [
            ("not json", "malformed JSON"),
            (_steps_json("a") + " trailing", "malformed JSON"),
            ("Here is the plan: " + _steps_json("a"), "malformed JSON"),
            ("```python\n" + _steps_json("a") + "\n```", "non-JSON code block"),
            ("```json\n" + _steps_json("a"), "unterminated"),
            (json.dumps([{"description": "a"}]), "exactly one key"),
            (json.dumps({"steps": [{"description": "a"}], "extra": 1}), "exactly one key"),
            (json.dumps({"steps": []}), "non-empty list"),
            (json.dumps({"steps": "a"}), "non-empty list"),
            (json.dumps({"steps": ["a"]}), "step 1"),
            (json.dumps({"steps": [{"description": "a", "tool_name": "write_file"}]}), "step 1"),
            (json.dumps({"steps": [{"description": "   "}]}), "empty description"),
            (json.dumps({"steps": [{"description": 3}]}), "empty description"),
            (json.dumps({"steps": [{"description": "x" * 2001}]}), "exceeds"),
            (_steps_json("1", "2", "3", "4"), "more than planner_max_steps"),
        ],
    )
    async def test_invalid_output_fails_closed(self, content, match):
        planner = ModelPlanner(provider=PlanProvider(content), max_steps=3)
        with pytest.raises(PlanningError, match=match):
            await planner.plan(Task(title="t", description="x"))

    def test_max_steps_follows_setting(self):
        with patch.object(settings, "planner_max_steps", 2):
            assert ModelPlanner(provider=PlanProvider()).max_steps == 2
            with pytest.raises(PlanningError, match=r"more than planner_max_steps \(2\)"):
                parse_model_plan(_steps_json("a", "b", "c"), ModelPlanner().max_steps)

    async def test_provider_exception_fails_closed_and_is_redacted(self):
        secret = "sk-" + "a" * 40
        planner = ModelPlanner(provider=PlanProvider(error=RuntimeError(f"auth failed for {secret}")), max_steps=3)
        with pytest.raises(PlanningError, match="Model planner failed") as excinfo:
            await planner.plan(Task(title="t", description="x"))
        assert secret not in str(excinfo.value) and "[REDACTED]" in str(excinfo.value)

    async def test_no_provider_fails_closed(self):
        with pytest.raises(PlanningError, match="no model provider is configured"):
            await ModelPlanner(max_steps=3).plan(Task(title="t", description="x"))

    async def test_unavailable_provider_fails_closed(self):
        with pytest.raises(PlanningError, match="not available"):
            await ModelPlanner(provider=PlanProvider(available=False), max_steps=3).plan(
                Task(title="t", description="x")
            )

    async def test_registry_default_provider_is_used(self):
        provider = PlanProvider(_steps_json("a"))
        provider_registry.register(provider, default=True)
        plan = await ModelPlanner(max_steps=3).plan(Task(title="t", description="x"))
        assert provider.calls == 1 and [s.description for s in plan.steps] == ["a"]


class TestPlannerSettings:
    def test_defaults(self):
        fresh = Settings()
        assert fresh.planner_mode == "rule" and fresh.planner_max_steps == 5
        validate_settings(fresh)

    def test_forwarded_to_workers(self):
        assert {"planner_mode", "planner_max_steps"} <= FORWARDABLE_SETTINGS

    @pytest.mark.parametrize("value", ["", "llm", "auto"])
    def test_unknown_mode_is_rejected(self, value):
        cfg = Settings()
        cfg.planner_mode = value
        with pytest.raises(ConfigurationError, match="planner_mode"):
            validate_settings(cfg)

    @pytest.mark.parametrize("value", [0, 21, 2.5, True, "5"])
    def test_max_steps_bounds(self, value):
        cfg = Settings()
        cfg.planner_max_steps = value  # type: ignore[assignment]
        with pytest.raises(ConfigurationError, match="planner_max_steps"):
            validate_settings(cfg)

    @pytest.mark.parametrize("value", [1, 20])
    def test_max_steps_bounds_inclusive(self, value):
        cfg = Settings()
        cfg.planner_max_steps = value
        validate_settings(cfg)

    def test_model_mode_without_key_warns(self):
        cfg = Settings(_env_file=None, openai_api_key=None, nvidia_api_key=None)  # type: ignore[call-arg]
        cfg.planner_mode = "model"
        assert any("planner_mode='model'" in w for w in validate_settings(cfg))


# ============================================================================
# Orchestration with durable workers
# ============================================================================


class TestMultiStepExecution:
    async def test_steps_run_in_order(self):
        agent = StepAgent()
        agent_registry.register(agent)
        provider = PlanProvider(_steps_json("one", "two", "three"))
        runtime = _runtime()
        runtime.startup()
        try:
            record = _submit()
            with _model_planner(provider), patch.object(settings, "planner_mode", "model"):
                assert await _execute_next(runtime) == TaskStatus.COMPLETED
        finally:
            runtime.shutdown()
        assert agent.ran == ["one", "two", "three"]
        assert agent.previous_seen == [[], [1], [1, 2]]
        stored = task_tracker.get_task(record.task_id)
        assert [s.description for s in stored.plan.steps] == ["one", "two", "three"]  # type: ignore[union-attr]
        created = _audit(AuditEventType.PLAN_CREATED)
        assert len(created) == 1
        assert created[0].details["step_count"] == 3 and created[0].details["planner_mode"] == "model"

    async def test_first_failed_step_stops_the_plan_and_is_named(self):
        agent = StepAgent()
        agent_registry.register(agent)
        provider = PlanProvider(_steps_json("one", "fail", "three"))
        runtime = _runtime()
        runtime.startup()
        try:
            record = _submit()
            with _model_planner(provider), patch.object(settings, "planner_mode", "model"):
                assert await _execute_next(runtime) == TaskStatus.FAILED
        finally:
            runtime.shutdown()
        assert agent.ran == ["one", "fail"]
        stored = task_tracker.get_task(record.task_id)
        assert stored.error == "Plan step 2 of 3 failed: boom"  # type: ignore[union-attr]
        assert stored.result.step_id == 2  # type: ignore[union-attr]

    async def test_single_step_error_text_is_unchanged(self):
        agent_registry.register(StepAgent())
        runtime = _runtime()
        runtime.startup()
        try:
            record = _submit("fail")
            assert await _execute_next(runtime) == TaskStatus.FAILED
        finally:
            runtime.shutdown()
        assert task_tracker.get_task(record.task_id).error == "boom"  # type: ignore[union-attr]

    async def test_planning_failure_fails_the_task_without_running_anything(self):
        agent = StepAgent()
        agent_registry.register(agent)
        runtime = _runtime()
        runtime.startup()
        try:
            record = _submit(priority=9)
            with _model_planner(PlanProvider("nonsense")), patch.object(settings, "planner_mode", "model"):
                assert await _execute_next(runtime) == TaskStatus.FAILED
        finally:
            runtime.shutdown()
        stored = task_tracker.get_task(record.task_id)
        assert "malformed JSON" in stored.error  # type: ignore[union-attr,operator]
        assert stored.plan is None and stored.approvals == [] and agent.ran == []  # type: ignore[union-attr]

    async def test_in_process_execute_task_names_failed_step(self):
        agent = StepAgent()
        agent_registry.register(agent)
        with _model_planner(PlanProvider(_steps_json("one", "fail"))), patch.object(settings, "planner_mode", "model"):
            result = await orchestrator.execute_task(Task(title="t", description="x", user_id=ENG))
        assert result is not None and result.error == "Plan step 2 of 2 failed: boom"
        failed = _audit(AuditEventType.TASK_FAILED)
        assert failed[-1].details["step_id"] == 2


    async def test_in_process_planning_failure_fails_the_task(self):
        task = Task(title="t", description="x", user_id=ENG)
        with _model_planner(PlanProvider("nonsense")), patch.object(settings, "planner_mode", "model"):
            with pytest.raises(PlanningError, match="malformed JSON"):
                await orchestrator.execute_task(task)
        stored = task_tracker.get_task(task.id)
        assert stored.status == TaskStatus.FAILED and "malformed JSON" in stored.error  # type: ignore[union-attr]


class TestPlanPersistenceAndApproval:
    async def test_plan_is_reused_across_park_and_resume(self):
        agent = StepAgent()
        agent_registry.register(agent)
        provider = PlanProvider(_steps_json("one", "two"))
        runtime = _runtime()
        runtime.startup()
        try:
            record = _submit(priority=9)
            with _model_planner(provider), patch.object(settings, "planner_mode", "model"):
                assert await _execute_next(runtime) == TaskStatus.AWAITING_APPROVAL
                assert agent.ran == []  # paused before any step ran
                # A different plan on resume would change the digest; the stored plan must win.
                provider.content = _steps_json("something", "else", "entirely")
                _approve_latest(record.task_id)
                assert await _execute_next(runtime) == TaskStatus.COMPLETED
        finally:
            runtime.shutdown()
        assert provider.calls == 1
        assert agent.ran == ["one", "two"]
        stored = task_tracker.get_task(record.task_id)
        assert [s.description for s in stored.plan.steps] == ["one", "two"]  # type: ignore[union-attr]
        [request] = stored.approvals  # type: ignore[union-attr]
        assert request.action_type == PLAN_APPROVAL_OPERATION and request.consumed_at is not None
        expected = operation_digest(
            PLAN_APPROVAL_OPERATION, "orchestrator", PLAN_APPROVAL_PERMISSION, plan_approval_arguments(stored.plan, 9)
        )
        assert request.operation_digest == expected
        assert len(_audit(AuditEventType.PLAN_CREATED)) == 1

    async def test_plan_approval_is_single_use(self):
        agent_registry.register(StepAgent())
        runtime = _runtime()
        runtime.startup()
        try:
            record = _submit(priority=9)
            assert await _execute_next(runtime) == TaskStatus.AWAITING_APPROVAL
            _approve_latest(record.task_id)
            assert await _execute_next(runtime) == TaskStatus.COMPLETED
        finally:
            runtime.shutdown()
        stored = task_tracker.get_task(record.task_id)
        args = plan_approval_arguments(stored.plan, 9)  # type: ignore[union-attr,arg-type]
        assert not approval_service.authorize_or_request(
            record.task_id,
            AgentId(id="orchestrator"),
            ENG,
            PLAN_APPROVAL_OPERATION,
            PLAN_APPROVAL_PERMISSION,
            args,
            "r",
        )

    async def test_legacy_stored_plan_with_step_approval_still_requires_approval(self):
        agent = StepAgent()
        agent_registry.register(agent)
        record = _submit("legacy", priority=9)
        # The JSON shape stored before plan-level approvals: no plan fields, approval on the step.
        legacy_plan = Plan(task_id=record.task_id, steps=[PlanStep(step_id=1, description="legacy")], reasoning="r")
        legacy_json = legacy_plan.model_dump(mode="json")
        del legacy_json["approval_required"], legacy_json["planner_mode"]
        legacy_json["steps"][0]["approval_required"] = True
        with db_manager.transaction() as conn:
            conn.execute(
                "UPDATE task_records SET plan = ? WHERE task_id = ?", (json.dumps(legacy_json), str(record.task_id))
            )
        provider = PlanProvider(_steps_json("new"))
        runtime = _runtime()
        runtime.startup()
        try:
            with _model_planner(provider), patch.object(settings, "planner_mode", "model"):
                assert await _execute_next(runtime) == TaskStatus.AWAITING_APPROVAL
                assert agent.ran == []
                _approve_latest(record.task_id)
                assert await _execute_next(runtime) == TaskStatus.COMPLETED
        finally:
            runtime.shutdown()
        assert provider.calls == 0 and agent.ran == ["legacy"]
        stored = task_tracker.get_task(record.task_id)
        assert stored.plan.planner_mode == "rule" and stored.plan.approval_required is False  # type: ignore[union-attr]
        assert stored.approvals[0].action_type == PLAN_APPROVAL_OPERATION  # type: ignore[union-attr]

    async def test_set_plan_if_absent_keeps_the_first_plan(self):
        record = _submit("x")
        first = Plan(task_id=record.task_id, steps=[PlanStep(step_id=1, description="x")], reasoning="first")
        stored, created = task_tracker.set_plan_if_absent(record.task_id, first)
        assert created is True and stored is not None
        second = first.model_copy(update={"reasoning": "other"})
        stored_again, created_again = task_tracker.set_plan_if_absent(record.task_id, second)
        assert created_again is False and stored_again.reasoning == first.reasoning  # type: ignore[union-attr]

    async def test_plan_and_tool_approvals_are_bounded_by_max_rounds(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        agent_registry.register(CodingAgent(provider=WriteFileProvider(), repo_root=str(repo)))
        for perm in CODING_AGENT_PERMISSIONS:
            permission_manager.grant_permission("coding-agent", perm)
        runtime = _runtime()
        runtime.startup()
        try:
            record = _submit("write a file", priority=9)
            with patch.object(settings, "max_approval_rounds", 1):
                assert await _execute_next(runtime) == TaskStatus.AWAITING_APPROVAL  # plan approval
                _approve_latest(record.task_id)
                assert await _execute_next(runtime) == TaskStatus.FAILED  # tool approval would be round 2
        finally:
            runtime.shutdown()
        stored = task_tracker.get_task(record.task_id)
        assert "max_approval_rounds (1)" in stored.error  # type: ignore[union-attr,operator]
        assert [a.action_type for a in stored.approvals] == ["plan", "tool:write_file"]  # type: ignore[union-attr]
        assert list(repo.iterdir()) == []
