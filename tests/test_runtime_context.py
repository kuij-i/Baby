"""Phase 18 F4: explicit runtime contexts instead of reaching for module-level singletons.

The payoff test runs two independent ``BabyContext`` instances, each on its own SQLite database,
side by side in one process: no ``patch()``, no autouse fixture resetting global singletons.
The remaining tests pin the invariants: the default context is the module singletons, contexts
never cross the worker spawn boundary, and each injectable component uses what it is given.
"""

import pickle
from pathlib import Path
from typing import Any, List, Tuple

import pytest

from baby.agents.coding import CODING_AGENT_PERMISSIONS, CodingAgent
from baby.configuration import settings
from baby.core import AgentId, AuditEventType, Task, TaskStatus, WorkerStatus
from baby.errors import PermissionDeniedError
from baby.persistence.sqlite_workers import SQLiteWorkerStore
from baby.runtime_context import BabyContext, build_default_context, close_context, default_context
from baby.tools.coding.list_files import ListFilesTool
from baby.workers.process import FORWARDABLE_SETTINGS, WorkerProcessConfig
from baby.workers.queue import DurableTaskQueue
from baby.workers.runtime import WorkerRuntime, WorkerRuntimeConfig, new_worker_id


def _context(tmp_path: Path, name: str, grant_permissions: bool) -> Tuple[BabyContext, Path]:
    """A context on its own database with a CodingAgent bounded to its own repository."""
    repo = tmp_path / f"repo-{name}"
    repo.mkdir()
    (repo / f"only-in-{name}.txt").write_text(name, encoding="utf-8")
    ctx = build_default_context(settings, f"sqlite:///{tmp_path / f'{name}.db'}")
    agent = CodingAgent(
        repo_root=str(repo),
        tool_executor=ctx.tool_executor,
        audit=ctx.audit_log,
        providers=ctx.provider_registry,
    )
    ctx.agent_registry.register(agent)
    if grant_permissions:
        for permission in CODING_AGENT_PERMISSIONS:
            ctx.permission_manager.grant_permission(str(agent.spec.id), permission)
    return ctx, repo


async def _run_one(ctx: BabyContext, description: str) -> Tuple[Any, TaskStatus]:
    store = ctx.task_tracker.store
    assert store is not None
    queue = DurableTaskQueue(store, SQLiteWorkerStore(store.db), lease_duration_seconds=30)
    record = queue.submit(Task(title="ctx", description=description, user_id="eng-ctx"))
    config = WorkerRuntimeConfig(
        worker_id=new_worker_id(),
        poll_interval=0.01,
        heartbeat_interval=0.02,
        stale_timeout=10.0,
        lease_duration=10.0,
        max_task_seconds=30.0,
    )
    runtime = WorkerRuntime(config, context=ctx)
    runtime.startup()
    try:
        claimed = runtime.queue.claim_next(runtime.worker_id)
        assert claimed is not None
        status = await runtime.execute_claimed(claimed)
    finally:
        runtime.shutdown()
    assert status is not None
    return record, status


def _task_ids(events: List[Any]) -> set:
    return {str(e.task_id) for e in events if e.task_id is not None}


class TestTwoIndependentContexts:
    async def test_two_contexts_run_side_by_side_without_sharing_state(self, tmp_path):
        from baby.audit import audit_log as global_audit_log

        ctx_a, repo_a = _context(tmp_path, "a", grant_permissions=True)
        ctx_b, _ = _context(tmp_path, "b", grant_permissions=False)
        try:
            record_a, status_a = await _run_one(ctx_a, "list files")
            record_b, status_b = await _run_one(ctx_b, "list files")

            # A ran its CodingAgent against its own repository.
            assert status_a == TaskStatus.COMPLETED
            stored_a = ctx_a.task_tracker.get_task(record_a.task_id)
            assert [e["path"] for e in stored_a.result.output["entries"]] == ["only-in-a.txt"]  # type: ignore

            # B's permission manager granted nothing, so B's executor denied the same request.
            assert status_b == TaskStatus.FAILED
            stored_b = ctx_b.task_tracker.get_task(record_b.task_id)
            assert "lacks local_read permission" in (stored_b.error or "")  # type: ignore[union-attr]

            # Each database holds only its own task, workers and audit trail.
            assert ctx_a.task_tracker.get_task(record_b.task_id) is None
            assert ctx_b.task_tracker.get_task(record_a.task_id) is None
            assert ctx_a.task_tracker.count_tasks() == 1 and ctx_b.task_tracker.count_tasks() == 1
            workers_a = SQLiteWorkerStore(ctx_a.db_manager).list()
            workers_b = SQLiteWorkerStore(ctx_b.db_manager).list()
            assert len(workers_a) == 1 and len(workers_b) == 1
            assert workers_a[0].worker_id != workers_b[0].worker_id
            assert workers_a[0].status == WorkerStatus.STOPPED
            assert _task_ids(ctx_a.audit_log.get_events(limit=1000)) == {str(record_a.task_id)}
            assert _task_ids(ctx_b.audit_log.get_events(limit=1000)) == {str(record_b.task_id)}
            invoked_a = ctx_a.audit_log.get_events(event_type=AuditEventType.TOOL_INVOKED, limit=1000)
            assert len(invoked_a) == 1
            assert ctx_b.audit_log.get_events(event_type=AuditEventType.TOOL_INVOKED, limit=1000) == []

            # Registries and permission managers are separate objects with separate contents.
            assert ctx_a.agent_registry is not ctx_b.agent_registry
            assert ctx_a.permission_manager.get_agent_permissions("coding-agent")
            assert not ctx_b.permission_manager.get_agent_permissions("coding-agent")

            # The process-wide default services saw none of it.
            assert not ({str(record_a.task_id), str(record_b.task_id)} & _task_ids(global_audit_log.get_events()))
            assert repo_a.exists()
        finally:
            close_context(ctx_a)
            close_context(ctx_b)


class TestDefaultContext:
    def test_default_context_is_the_module_singletons(self):
        from baby.agents import agent_registry
        from baby.audit import audit_log
        from baby.observability import task_tracker
        from baby.orchestration.orchestrator import orchestrator
        from baby.permissions import permission_manager
        from baby.permissions.approvals import approval_service
        from baby.persistence.database import db_manager
        from baby.tools.executor import tool_executor

        ctx = default_context()
        assert ctx.settings is settings
        assert ctx.audit_log is audit_log and ctx.task_tracker is task_tracker
        assert ctx.orchestrator is orchestrator and ctx.tool_executor is tool_executor
        assert ctx.agent_registry is agent_registry and ctx.permission_manager is permission_manager
        assert ctx.approval_service is approval_service and ctx.db_manager is db_manager

    def test_built_context_is_wired_to_itself(self, tmp_path):
        ctx = build_default_context(settings, f"sqlite:///{tmp_path / 'wired.db'}")
        try:
            assert ctx.orchestrator.task_tracker is ctx.task_tracker
            assert ctx.orchestrator.audit is ctx.audit_log
            assert ctx.orchestrator.agents is ctx.agent_registry
            assert ctx.orchestrator.approvals is ctx.approval_service
            assert ctx.orchestrator.planner is ctx.task_planner
            assert ctx.selection_logic.agents is ctx.agent_registry
            assert ctx.tool_executor.permissions is ctx.permission_manager
            assert ctx.tool_executor.approvals is ctx.approval_service
            assert ctx.approval_service._store() is ctx.task_tracker.store
            assert ctx.task_tracker.store.db is ctx.db_manager  # type: ignore[union-attr]
        finally:
            close_context(ctx)


class TestPerProcessInvariant:
    def test_contexts_cannot_be_pickled(self, tmp_path):
        ctx = build_default_context(settings, f"sqlite:///{tmp_path / 'p.db'}")
        try:
            with pytest.raises(TypeError, match="per process"):
                pickle.dumps(ctx)
            with pytest.raises(TypeError, match="per process"):
                pickle.dumps(default_context())
        finally:
            close_context(ctx)

    def test_worker_process_config_carries_no_context_or_secrets(self):
        fields = set(WorkerProcessConfig.__dataclass_fields__)
        assert not any("context" in name for name in fields)
        for secret in ("openai_api_key", "nvidia_api_key", "api_auth_token", "api_admin_token"):
            assert secret not in FORWARDABLE_SETTINGS


class TestInjectedDependencies:
    async def test_tool_executor_uses_its_permission_manager(self, tmp_path):
        from baby.permissions.manager import PermissionManager
        from baby.tools.executor import ToolExecutor

        (tmp_path / "x.txt").write_text("x", encoding="utf-8")
        executor = ToolExecutor(permissions=PermissionManager())
        with pytest.raises(PermissionDeniedError):
            await executor.execute(ListFilesTool(repo_root=str(tmp_path)), AgentId(id="nobody"))

    def test_defaults_resolve_to_singletons_at_use_time(self, tmp_path):
        from baby.agents import agent_registry
        from baby.audit import audit_log
        from baby.orchestration.orchestrator import Orchestrator
        from baby.permissions.approvals import approval_service
        from baby.persistence.database import DatabaseManager
        from baby.persistence.sqlite_tasks import SQLiteTaskStore
        from baby.planning.selection import SelectionLogic
        from baby.tools.executor import ToolExecutor
        from baby.workers.recovery import WorkerRecovery

        assert Orchestrator().audit is audit_log and Orchestrator().approvals is approval_service
        assert ToolExecutor().audit is audit_log
        assert SelectionLogic().agents is agent_registry
        assert CodingAgent(repo_root=".").audit is audit_log
        db = DatabaseManager(f"sqlite:///{tmp_path / 'r.db'}")
        try:
            assert WorkerRecovery(SQLiteTaskStore(db)).audit is audit_log
        finally:
            db.close()
