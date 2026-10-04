"""Phase 12: durable worker architecture and process isolation.

Covers worker registration/identity, heartbeats and stale detection, atomic task
claiming with fenced leases, deterministic concurrency (threads and processes behind
barriers), fail-closed recovery (crash, stale worker, expired lease, DB failure,
restarts), cancellation, bounded execution, approval enforcement, API task control,
security boundaries, and real spawned worker processes.
"""

import asyncio
import multiprocessing
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from baby.agents import agent_registry
from baby.agents.coding import CODING_AGENT_PERMISSIONS, CodingAgent
from baby.api.app import app, lifespan
from baby.audit import audit_log
from baby.configuration import Settings, settings, validate_settings
from baby.core import (
    AgentId,
    AuditEventType,
    PermissionCategory,
    PermissionLevel,
    Task,
    TaskStatus,
    ToolPermission,
    ToolSpec,
    WorkerStatus,
)
from baby.errors import (
    ConfigurationError,
    InvalidStateTransitionError,
    LeaseLostError,
    PermissionDeniedError,
)
from baby.observability import metrics_collector, task_tracker, worker_tracker
from baby.permissions import permission_manager
from baby.permissions.approvals import approval_service
from baby.persistence.database import DatabaseManager, db_manager
from baby.persistence.sqlite_audit import SQLiteAuditLog
from baby.persistence.sqlite_tasks import SQLiteTaskStore
from baby.persistence.sqlite_workers import SQLiteWorkerStore, sanitize_worker_metadata
from baby.tools.base import Tool, ToolResult
from baby.tools.executor import tool_executor
from baby.workers import (
    DurableTaskQueue,
    WorkerRecovery,
    WorkerRuntime,
    WorkerRuntimeConfig,
    default_bootstrap,
    new_worker_id,
)
from baby.workers.process import EXIT_CONFIG, WORKER_PROCESS_ENV, WorkerProcessConfig, _run
from baby.workers.supervisor import WorkerSupervisor
from tests.worker_helpers import WriteFileProvider, bootstrap_scripted_agent, race_claim

ENG_A = "eng-A"
ENG_B = "eng-B"
SCRIPTED_BOOTSTRAP = "tests.worker_helpers:bootstrap_scripted_agent"
FAST_WORKER_SETTINGS = {
    "worker_poll_interval_seconds": 0.05,
    "worker_heartbeat_interval_seconds": 0.1,
    "worker_stale_timeout_seconds": 2.0,
    "task_lease_duration_seconds": 2.0,
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """Deterministic clock for lease/heartbeat timing (no sleeps)."""

    def __init__(self) -> None:
        self.now = datetime.utcnow()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def durable_env(tmp_path: Path):
    """Point every singleton at an isolated, file-backed SQLite database."""
    original_url = db_manager.database_url
    db_file = tmp_path / "baby.db"
    db_manager.configure(f"sqlite:///{db_file}")
    db_manager.init_schema()
    for reset in (task_tracker.clear, audit_log.clear, agent_registry.clear, permission_manager.clear):
        reset()
    worker_tracker.clear()
    metrics_collector.reset()
    yield db_file
    audit_log.use_backend(None)
    audit_log.clear()
    agent_registry.clear()
    permission_manager.clear()
    db_manager.configure(original_url)
    db_manager.init_schema()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def stores():
    task_store = task_tracker.store
    assert task_store is not None
    return task_store, SQLiteWorkerStore(task_store.db)


def _queue(stores, clock: Callable[[], datetime] = datetime.utcnow, lease: float = 30.0) -> DurableTaskQueue:
    return DurableTaskQueue(stores[0], stores[1], lease_duration_seconds=lease, clock=clock)


def _recovery(stores, clock: Callable[[], datetime] = datetime.utcnow, stale: float = 10.0) -> WorkerRecovery:
    return WorkerRecovery(stores[0], stores[1], stale_timeout_seconds=stale, clock=clock)


def _register(stores, worker_id: str, now: Optional[datetime] = None) -> str:
    now = now or datetime.utcnow()
    stores[1].register(worker_id, hostname="test-host", pid=1234, now=now)
    stores[1].set_status(worker_id, WorkerStatus.IDLE, now)
    return worker_id


def _submit(queue: DurableTaskQueue, description: str = "ok", priority: int = 0, user: str = ENG_A):
    return queue.submit(Task(title=f"task {description}", description=description, priority=priority, user_id=user))


def _runtime_config(**overrides: Any) -> WorkerRuntimeConfig:
    values = dict(
        worker_id=new_worker_id(),
        poll_interval=0.01,
        heartbeat_interval=0.02,
        stale_timeout=10.0,
        lease_duration=10.0,
        max_task_seconds=30.0,
    )
    values.update(overrides)
    return WorkerRuntimeConfig(**values)  # type: ignore[arg-type]


def _wait_for(predicate: Callable[[], bool], timeout: float = 45.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _status(task_id) -> TaskStatus:  # type: ignore[no-untyped-def]
    record = task_tracker.get_task(task_id)
    assert record is not None
    return record.status


# ============================================================================
# 1. SCHEMA, MIGRATION AND CONSTRAINTS
# ============================================================================


class TestSchema:
    def test_v1_database_is_migrated_in_place(self, tmp_path: Path):
        legacy = DatabaseManager(f"sqlite:///{tmp_path / 'legacy.db'}")
        with legacy.transaction() as conn:
            conn.execute("""
                CREATE TABLE task_records (
                    task_id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    completed_at TEXT, user_id TEXT, priority INTEGER NOT NULL DEFAULT 0,
                    assigned_agent_id TEXT, assigned_worker_id TEXT, plan TEXT, result TEXT,
                    error TEXT, approvals TEXT, verifications TEXT, retry_count INTEGER NOT NULL DEFAULT 0
                )""")
            now = datetime.utcnow().isoformat()
            conn.execute(
                "INSERT INTO task_records (task_id, title, description, status, created_at, updated_at, approvals,"
                " verifications) VALUES (?, 't', 'd', 'pending', ?, ?, '[]', '[]')",
                (str(uuid4()), now, now),
            )
        legacy.init_schema()
        legacy.init_schema()  # idempotent
        assert legacy.schema_version() == 2
        with legacy.transaction() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(task_records)")}
            tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            row = conn.execute("SELECT status, cancel_requested, claim_count FROM task_records").fetchone()
        assert {"cancel_requested", "claim_count"} <= cols
        assert {"workers", "task_leases"} <= tables
        assert (row["status"], row["cancel_requested"], row["claim_count"]) == ("pending", 0, 0)
        legacy.close()

    def test_worker_status_check_constraint(self, stores):
        with pytest.raises(sqlite3.IntegrityError):
            with stores[0].db.transaction() as conn:
                conn.execute(
                    "INSERT INTO workers (worker_id, hostname, pid, status, started_at, last_heartbeat_at)"
                    " VALUES ('w', 'h', 1, 'bogus', 'x', 'x')"
                )

    def test_at_most_one_lease_per_task(self, stores, clock):
        queue = _queue(stores, clock)
        worker = _register(stores, "w-1", clock())
        record = _submit(queue)
        assert queue.claim_next(worker) is not None
        with pytest.raises(sqlite3.IntegrityError):
            with stores[0].db.transaction() as conn:
                conn.execute("INSERT INTO task_leases VALUES (?, ?, 99, 'x', 'x', 'x')", (str(record.task_id), worker))

    def test_lease_requires_registered_worker(self, stores):
        record = _submit(_queue(stores))
        with pytest.raises(sqlite3.IntegrityError):
            with stores[0].db.transaction() as conn:
                conn.execute("INSERT INTO task_leases VALUES (?, 'ghost', 1, 'x', 'x', 'x')", (str(record.task_id),))


# ============================================================================
# 2. WORKER REGISTRATION AND IDENTITY
# ============================================================================


class TestWorkerRegistration:
    def test_worker_ids_are_unique_and_carry_no_host_info(self):
        ids = {new_worker_id() for _ in range(500)}
        assert len(ids) == 500
        import socket

        assert all(socket.gethostname() not in i for i in ids)

    def test_register_starts_in_starting_state(self, stores, clock):
        info = stores[1].register("w-1", hostname="host", pid=42, now=clock())
        assert info.status == WorkerStatus.STARTING
        assert info.metadata["pid"] == 42 and info.metadata["hostname"] == "host"
        assert info.started_at == clock() and info.last_heartbeat_at == clock()

    def test_worker_identity_is_never_reused(self, stores, clock):
        stores[1].register("w-1", hostname="host", pid=1, now=clock())
        with pytest.raises(sqlite3.IntegrityError):
            stores[1].register("w-1", hostname="host", pid=2, now=clock())

    def test_metadata_never_stores_secrets(self, stores, clock):
        info = stores[1].register(
            "w-1", hostname="h", pid=1, now=clock(), metadata={"api_key": "sk-secret", "token": "t", "slot": 3}
        )
        assert info.metadata == {"hostname": "h", "pid": 1, "slot": 3}
        assert sanitize_worker_metadata({"password": "x", "env": {"A": "B"}}) == {}

    async def test_runtime_startup_and_shutdown_states(self, stores):
        runtime = WorkerRuntime(_runtime_config())
        runtime.startup()
        try:
            info = stores[1].get(runtime.worker_id)
            assert info is not None and info.status == WorkerStatus.IDLE
        finally:
            runtime.shutdown()
        info = stores[1].get(runtime.worker_id)
        assert info is not None and info.status == WorkerStatus.STOPPED and info.stopped_at is not None
        events = [e.event_type for e in audit_log.get_events()]
        assert AuditEventType.WORKER_STARTED in events and AuditEventType.WORKER_STOPPED in events

    def test_startup_fails_closed_when_recovery_fails(self, stores):
        runtime = WorkerRuntime(_runtime_config())
        with patch.object(WorkerRecovery, "recover", side_effect=sqlite3.OperationalError("db down")):
            with pytest.raises(RuntimeError):
                runtime.startup()
        info = stores[1].get(runtime.worker_id)
        assert info is not None and info.status == WorkerStatus.ERROR
        _submit(_queue(stores))
        # An errored identity can never claim work.
        assert runtime.queue.claim_next(runtime.worker_id) is None

    def test_worker_process_requires_file_database(self):
        code = _run(WorkerProcessConfig(worker_id=new_worker_id(), database_url=":memory:"), None)
        assert code == EXIT_CONFIG

    def test_worker_process_rejects_unforwardable_settings(self, durable_env):
        cfg = WorkerProcessConfig(
            worker_id=new_worker_id(),
            database_url=f"sqlite:///{durable_env}",
            settings_overrides={"openai_api_key": "sk-x"},
        )
        assert _run(cfg, None) == EXIT_CONFIG


# ============================================================================
# 3. HEARTBEATS AND STALE DETECTION
# ============================================================================


class TestHeartbeats:
    def test_heartbeat_updates_timestamp(self, stores, clock):
        _register(stores, "w-1", clock())
        clock.advance(3)
        assert stores[1].heartbeat("w-1", clock()) is True
        info = stores[1].get("w-1")
        assert info is not None and info.last_heartbeat_at == clock()

    def test_stale_worker_detected_deterministically(self, stores, clock):
        _register(stores, "w-old", clock())
        _register(stores, "w-fresh", clock())
        clock.advance(11)
        stores[1].heartbeat("w-fresh", clock())
        report = _recovery(stores, clock, stale=10).recover()
        assert report.stale_workers == ["w-old"]
        assert stores[1].get("w-old").status == WorkerStatus.STALE  # type: ignore[union-attr]
        assert stores[1].get("w-fresh").status == WorkerStatus.IDLE  # type: ignore[union-attr]
        assert any(e.event_type == AuditEventType.WORKER_STALE for e in audit_log.get_events())

    def test_boundary_heartbeat_exactly_at_timeout_is_not_stale(self, stores, clock):
        _register(stores, "w-1", clock())
        clock.advance(10)
        assert _recovery(stores, clock, stale=10).recover().stale_workers == []

    def test_stale_worker_identity_is_revoked(self, stores, clock):
        runtime = WorkerRuntime(_runtime_config(heartbeat_interval=60), clock=clock)
        runtime.startup()
        try:
            clock.advance(11)
            _recovery(stores, clock, stale=10).recover()
            assert runtime.heartbeat_once() is False
            assert runtime.fatal_error is not None
            assert runtime.stop_requested()
        finally:
            runtime.shutdown()
        # Shutdown never "revives" a stale worker.
        assert stores[1].get(runtime.worker_id).status == WorkerStatus.STALE  # type: ignore[union-attr]

    def test_heartbeat_database_failure_is_audited_and_becomes_fatal(self, stores):
        runtime = WorkerRuntime(_runtime_config(heartbeat_interval=0.01, stale_timeout=0.05))
        runtime.startup()
        try:
            with patch.object(SQLiteWorkerStore, "heartbeat", side_effect=sqlite3.OperationalError("disk I/O error")):
                assert _wait_for(lambda: runtime.fatal_error is not None, timeout=5)
        finally:
            runtime.shutdown()
        assert any(e.event_type == AuditEventType.WORKER_HEARTBEAT_FAILED for e in audit_log.get_events())
        assert stores[1].get(runtime.worker_id).status == WorkerStatus.ERROR  # type: ignore[union-attr]


# ============================================================================
# 4. TASK CLAIMING AND LEASES
# ============================================================================


class TestTaskClaiming:
    def test_submit_is_durable_pending_and_not_executed(self, stores):
        record = _submit(_queue(stores))
        assert record.status == TaskStatus.PENDING
        assert SQLiteTaskStore(stores[0].db).get_task(record.task_id).status == TaskStatus.PENDING  # type: ignore

    def test_duplicate_submission_rejected(self, stores):
        queue = _queue(stores)
        task = Task(title="t", description="d", user_id=ENG_A)
        queue.submit(task)
        with pytest.raises(InvalidStateTransitionError):
            queue.submit(task)

    def test_successful_claim_creates_lease(self, stores, clock):
        queue = _queue(stores, clock, lease=30)
        worker = _register(stores, "w-1", clock())
        record = _submit(queue)
        claimed = queue.claim_next(worker)
        assert claimed is not None
        assert claimed.record.task_id == record.task_id
        assert claimed.record.status == TaskStatus.IN_PROGRESS
        assert claimed.lease.fence == 1 and claimed.lease.worker_id == worker
        assert claimed.lease.lease_expires_at == clock() + timedelta(seconds=30)
        stored = task_tracker.get_task(record.task_id)
        assert stored.status == TaskStatus.IN_PROGRESS and stored.assigned_worker_id == worker  # type: ignore
        assert queue.get_lease(record.task_id) == claimed.lease
        info = stores[1].get(worker)
        assert info.status == WorkerStatus.BUSY and info.current_task_id == record.task_id  # type: ignore

    def test_duplicate_claim_rejected(self, stores, clock):
        queue = _queue(stores, clock)
        _submit(queue)
        assert queue.claim_next(_register(stores, "w-A", clock())) is not None
        assert queue.claim_next(_register(stores, "w-B", clock())) is None

    def test_claim_order_priority_then_age(self, stores, clock):
        queue = _queue(stores, clock)
        low = _submit(queue, "low", priority=1)
        high = _submit(queue, "high", priority=7)
        worker = _register(stores, "w-1", clock())
        first = queue.claim_next(worker)
        assert first is not None and first.record.task_id == high.task_id
        stores[1].set_status(worker, WorkerStatus.IDLE, clock())
        second = queue.claim_next(worker)
        assert second is not None and second.record.task_id == low.task_id

    def test_unregistered_stale_or_stopping_workers_cannot_claim(self, stores, clock):
        queue = _queue(stores, clock)
        _submit(queue)
        assert queue.claim_next("ghost-worker") is None
        stale = _register(stores, "w-stale", clock())
        stores[1].set_status(stale, WorkerStatus.STALE, clock())
        assert queue.claim_next(stale) is None
        stopping = _register(stores, "w-stopping", clock())
        stores[1].set_status(stopping, WorkerStatus.STOPPING, clock())
        assert queue.claim_next(stopping) is None

    def test_lease_renewal_extends_expiry(self, stores, clock):
        queue = _queue(stores, clock, lease=30)
        worker = _register(stores, "w-1", clock())
        _submit(queue)
        claimed = queue.claim_next(worker)
        assert claimed is not None
        clock.advance(20)
        state = queue.renew(claimed.record.task_id, worker, claimed.lease.fence)
        assert state.held and state.lease_expires_at == clock() + timedelta(seconds=30)
        clock.advance(25)  # past the original expiry, within the renewed one
        assert queue.renew(claimed.record.task_id, worker, claimed.lease.fence).held

    def test_lease_expiration_blocks_renewal_and_result(self, stores, clock):
        queue = _queue(stores, clock, lease=30)
        worker = _register(stores, "w-1", clock())
        _submit(queue)
        claimed = queue.claim_next(worker)
        assert claimed is not None
        clock.advance(31)
        assert queue.renew(claimed.record.task_id, worker, claimed.lease.fence).held is False
        with pytest.raises(LeaseLostError):
            queue.finalize(claimed.record.task_id, worker, claimed.lease.fence, TaskStatus.COMPLETED)
        assert _status(claimed.record.task_id) == TaskStatus.IN_PROGRESS  # no false success

    def test_finalize_releases_lease_and_counts(self, stores, clock):
        queue = _queue(stores, clock)
        worker = _register(stores, "w-1", clock())
        _submit(queue)
        claimed = queue.claim_next(worker)
        assert claimed is not None
        record = queue.finalize(claimed.record.task_id, worker, claimed.lease.fence, TaskStatus.COMPLETED)
        assert record.status == TaskStatus.COMPLETED and record.completed_at is not None
        assert queue.get_lease(record.task_id) is None
        info = stores[1].get(worker)
        assert info.tasks_completed == 1 and info.status == WorkerStatus.IDLE  # type: ignore[union-attr]
        with pytest.raises(LeaseLostError):  # terminal: cannot be finalized again
            queue.finalize(record.task_id, worker, claimed.lease.fence, TaskStatus.FAILED)

    def test_finalize_rejects_non_terminal_status(self, stores, clock):
        queue = _queue(stores, clock)
        worker = _register(stores, "w-1", clock())
        _submit(queue)
        claimed = queue.claim_next(worker)
        with pytest.raises(InvalidStateTransitionError):
            queue.finalize(claimed.record.task_id, worker, claimed.lease.fence, TaskStatus.PENDING)  # type: ignore

    def test_cancelled_pending_task_is_never_claimed(self, stores, clock):
        queue = _queue(stores, clock)
        record = _submit(queue)
        assert queue.request_cancel(record.task_id).status == TaskStatus.CANCELLED
        assert queue.claim_next(_register(stores, "w-1", clock())) is None
        with pytest.raises(InvalidStateTransitionError):
            queue.request_cancel(record.task_id)


# ============================================================================
# 5. CONCURRENCY (deterministic barriers, separate connections / processes)
# ============================================================================


class TestConcurrency:
    def test_two_workers_competing_for_one_task_threads(self, durable_env):
        url = f"sqlite:///{durable_env}"
        setup = DatabaseManager(url)
        setup_stores = (SQLiteTaskStore(setup), SQLiteWorkerStore(setup))
        for wid in ("race-A", "race-B"):
            _register(setup_stores, wid)

        for _ in range(25):
            record = _submit(_queue(setup_stores))
            barrier = threading.Barrier(2)
            results: dict = {}

            def contender(worker_id: str) -> None:
                db = DatabaseManager(url)  # own connection, like a separate process
                queue = DurableTaskQueue(SQLiteTaskStore(db), SQLiteWorkerStore(db), lease_duration_seconds=60)
                barrier.wait()
                results[worker_id] = queue.claim_next(worker_id)
                db.close()

            threads = [threading.Thread(target=contender, args=(w,)) for w in ("race-A", "race-B")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)
            winners = [w for w, claimed in results.items() if claimed is not None]
            assert len(winners) == 1, results
            assert results[winners[0]].record.task_id == record.task_id
            for wid in ("race-A", "race-B"):
                setup_stores[1].set_status(wid, WorkerStatus.IDLE, datetime.utcnow())
        setup.close()

    def test_competing_worker_processes_claim_each_task_exactly_once(self, durable_env, stores):
        queue = _queue(stores)
        submitted = {str(_submit(queue, f"t{i}").task_id) for i in range(12)}
        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(3)
        results = ctx.Queue()
        url = f"sqlite:///{durable_env}"
        procs = [ctx.Process(target=race_claim, args=(url, f"proc-{i}", barrier, results, 12)) for i in range(3)]
        for p in procs:
            p.start()
        claims = [results.get(timeout=60) for _ in procs]
        for p in procs:
            p.join(timeout=30)
        claimed = [task for _, tasks in claims for task in tasks]
        assert len(claimed) == len(set(claimed)) == len(submitted)
        assert set(claimed) == submitted
        with stores[0].db.transaction() as conn:
            counts = {r["claim_count"] for r in conn.execute("SELECT claim_count FROM task_records")}
            leases = conn.execute("SELECT COUNT(*) FROM task_leases").fetchone()[0]
        assert counts == {1} and leases == len(submitted)

    def test_lease_expiry_then_other_worker_observes_stale_claim(self, stores, clock):
        queue = _queue(stores, clock, lease=30)
        worker_a = _register(stores, "w-A", clock())
        _submit(queue)
        claimed = queue.claim_next(worker_a)
        assert claimed is not None
        # Worker A stops renewing. Time passes beyond both the lease and the stale timeout.
        clock.advance(31)
        worker_b = _register(stores, "w-B", clock())
        report = _recovery(stores, clock, stale=10).recover()
        assert report.stale_workers == [worker_a]
        assert [r.task_id for r in report.failed_tasks] == [claimed.record.task_id]
        record = task_tracker.get_task(claimed.record.task_id)
        assert record.status == TaskStatus.FAILED and "not replayed" in record.error  # type: ignore
        # B does not re-run the interrupted task, and A can no longer write a result.
        assert queue.claim_next(worker_b) is None
        with pytest.raises(LeaseLostError):
            queue.finalize(claimed.record.task_id, worker_a, claimed.lease.fence, TaskStatus.COMPLETED)
        assert _recovery(stores, clock, stale=10).recover().changed is False  # idempotent


# ============================================================================
# 6. EXECUTION IN THE WORKER RUNTIME
# ============================================================================


class TestWorkerExecution:
    @pytest.fixture(autouse=True)
    def _scripted(self):
        bootstrap_scripted_agent()

    async def _run_one(self, stores, description: str, **cfg: Any):
        runtime = WorkerRuntime(_runtime_config(**cfg))
        runtime.startup()
        try:
            record = _submit(runtime.queue, description)
            claimed = runtime.queue.claim_next(runtime.worker_id)
            assert claimed is not None
            status = await runtime.execute_claimed(claimed)
        finally:
            runtime.shutdown()
        return runtime, record, status

    async def test_success_completes_with_result(self, stores):
        runtime, record, status = await self._run_one(stores, "hello")
        assert status == TaskStatus.COMPLETED
        stored = task_tracker.get_task(record.task_id)
        assert stored.result.output == {"echo": "hello"} and stored.plan is not None  # type: ignore
        assert stores[1].get(runtime.worker_id).tasks_completed == 1  # type: ignore[union-attr]
        types = [e.event_type for e in audit_log.get_events(task_id=record.task_id)]
        assert AuditEventType.TASK_CLAIMED in types and AuditEventType.TASK_COMPLETED in types

    async def test_agent_failure_is_recorded(self, stores):
        _, record, status = await self._run_one(stores, "fail")
        assert status == TaskStatus.FAILED
        assert task_tracker.get_task(record.task_id).error == "boom"  # type: ignore[union-attr]

    async def test_agent_exception_is_recorded(self, stores):
        _, record, status = await self._run_one(stores, "raise")
        assert status == TaskStatus.FAILED
        assert "agent exploded" in task_tracker.get_task(record.task_id).error  # type: ignore[union-attr]

    async def test_bounded_execution(self, stores):
        _, record, status = await self._run_one(stores, "sleep:5", max_task_seconds=0.05)
        assert status == TaskStatus.FAILED
        assert "worker_max_task_seconds" in task_tracker.get_task(record.task_id).error  # type: ignore

    async def test_cancellation_while_in_progress(self, stores):
        runtime = WorkerRuntime(_runtime_config())
        runtime.startup()
        try:
            record = _submit(runtime.queue, "sleep:5")
            claimed = runtime.queue.claim_next(runtime.worker_id)

            async def cancel_soon():
                await asyncio.sleep(0.05)
                return runtime.queue.request_cancel(record.task_id)

            status, requested = await asyncio.gather(runtime.execute_claimed(claimed), cancel_soon())
        finally:
            runtime.shutdown()
        assert requested.cancel_requested is True and requested.status == TaskStatus.IN_PROGRESS
        assert status == TaskStatus.CANCELLED
        assert _status(record.task_id) == TaskStatus.CANCELLED

    async def test_shutdown_during_execution_fails_closed(self, stores):
        runtime = WorkerRuntime(_runtime_config())
        runtime.startup()
        try:
            record = _submit(runtime.queue, "sleep:5")
            claimed = runtime.queue.claim_next(runtime.worker_id)

            async def stop_soon():
                await asyncio.sleep(0.05)
                runtime.request_stop()

            status, _ = await asyncio.gather(runtime.execute_claimed(claimed), stop_soon())
        finally:
            runtime.shutdown()
        assert status == TaskStatus.FAILED
        stored = task_tracker.get_task(record.task_id)
        assert stored.status == TaskStatus.FAILED and "shut down" in stored.error  # type: ignore[union-attr]
        assert stores[1].get(runtime.worker_id).status == WorkerStatus.STOPPED  # type: ignore[union-attr]

    async def test_lease_revoked_during_execution_writes_no_result(self, stores):
        runtime = WorkerRuntime(_runtime_config())
        runtime.startup()
        try:
            record = _submit(runtime.queue, "sleep:0.3")
            claimed = runtime.queue.claim_next(runtime.worker_id)

            async def revoke():
                await asyncio.sleep(0.05)
                _recovery(stores).recover_worker(runtime.worker_id)

            status, _ = await asyncio.gather(runtime.execute_claimed(claimed), revoke())
        finally:
            runtime.shutdown()
        assert status is None  # nothing written by the worker
        stored = task_tracker.get_task(record.task_id)
        assert stored.status == TaskStatus.FAILED and "not replayed" in stored.error  # type: ignore[union-attr]
        assert any(e.event_type == AuditEventType.TASK_LEASE_LOST for e in audit_log.get_events())

    async def test_lease_renewal_database_failure_stops_execution(self, stores):
        runtime = WorkerRuntime(_runtime_config())
        runtime.startup()
        try:
            record = _submit(runtime.queue, "sleep:5")
            claimed = runtime.queue.claim_next(runtime.worker_id)
            with patch.object(runtime.queue, "renew", side_effect=sqlite3.OperationalError("database is locked")):
                status = await asyncio.wait_for(runtime.execute_claimed(claimed), timeout=5)
        finally:
            runtime.shutdown()
        assert status is None
        # No false success; the abandoned lease is reclaimed by recovery and the task fails closed.
        assert _status(record.task_id) == TaskStatus.IN_PROGRESS
        later = FakeClock()
        later.advance(60)
        _recovery(stores, later).recover()
        assert _status(record.task_id) == TaskStatus.FAILED

    async def test_run_loop_processes_queue_and_stops(self, stores):
        queue = _queue(stores)
        ids = [_submit(queue, f"job-{i}").task_id for i in range(3)]
        runtime = WorkerRuntime(_runtime_config(max_tasks=3))
        assert await asyncio.wait_for(runtime.run(), timeout=20) == 0
        assert all(_status(i) == TaskStatus.COMPLETED for i in ids)
        assert stores[1].get(runtime.worker_id).status == WorkerStatus.STOPPED  # type: ignore[union-attr]


# ============================================================================
# 7. APPROVALS (never bypassed, never manufactured, single use)
# ============================================================================


class TestApprovals:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Path:
        root = tmp_path / "repo"
        root.mkdir()
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        return root

    def _coding_agent(self, repo: Path, path: str = "approved.py") -> WriteFileProvider:
        provider = WriteFileProvider(path=path)
        agent_registry.register(CodingAgent(provider=provider, repo_root=str(repo)))
        for perm in CODING_AGENT_PERMISSIONS:
            permission_manager.grant_permission("coding-agent", perm)
        return provider

    async def _execute_next(self, runtime: WorkerRuntime) -> Optional[TaskStatus]:
        claimed = runtime.queue.claim_next(runtime.worker_id)
        assert claimed is not None
        return await runtime.execute_claimed(claimed)

    async def test_coding_write_parks_then_resumes_after_admin_approval(self, stores, repo):
        self._coding_agent(repo)
        runtime = WorkerRuntime(_runtime_config())
        runtime.startup()
        try:
            record = _submit(runtime.queue, "write a file")
            assert await self._execute_next(runtime) == TaskStatus.AWAITING_APPROVAL
            assert not (repo / "approved.py").exists()
            parked = task_tracker.get_task(record.task_id)
            assert parked.status == TaskStatus.AWAITING_APPROVAL  # type: ignore[union-attr]
            (request,) = parked.approvals  # type: ignore[union-attr]
            assert request.action_type == "tool:write_file" and request.approved is None
            assert request.engagement_id == ENG_A and request.permission == "local_write"
            assert request.operation_summary["path"] == "approved.py"
            assert runtime.queue.get_lease(record.task_id) is None
            assert runtime.queue.claim_next(runtime.worker_id) is None  # undecided → not claimable

            approval_service.decide(record.task_id, request.request_id, True, decided_by="admin-user")
            claimed = runtime.queue.claim_next(runtime.worker_id)
            assert claimed is not None and claimed.resumed_after_approval and claimed.lease.fence == 2
            assert await runtime.execute_claimed(claimed) == TaskStatus.COMPLETED
        finally:
            runtime.shutdown()
        assert (repo / "approved.py").read_text(encoding="utf-8") == "x = 1\n"
        final = task_tracker.get_task(record.task_id)
        assert final.status == TaskStatus.COMPLETED  # type: ignore[union-attr]
        assert final.approvals[0].consumed_at is not None  # type: ignore[union-attr]

    async def test_denied_approval_fails_task_and_writes_nothing(self, stores, repo):
        self._coding_agent(repo)
        runtime = WorkerRuntime(_runtime_config())
        runtime.startup()
        try:
            record = _submit(runtime.queue, "write a file")
            await self._execute_next(runtime)
            request = task_tracker.get_task(record.task_id).approvals[0]  # type: ignore[union-attr]
            denied = approval_service.decide(record.task_id, request.request_id, False, "admin-user", reason="no")
            assert denied.status == TaskStatus.FAILED and "denied" in denied.error  # type: ignore[operator]
            assert runtime.queue.claim_next(runtime.worker_id) is None
        finally:
            runtime.shutdown()
        assert not (repo / "approved.py").exists()

    async def test_crash_after_approval_never_replays_the_write(self, stores, repo):
        self._coding_agent(repo)
        runtime = WorkerRuntime(_runtime_config())
        runtime.startup()
        try:
            record = _submit(runtime.queue, "write a file")
            await self._execute_next(runtime)
            request = task_tracker.get_task(record.task_id).approvals[0]  # type: ignore[union-attr]
            approval_service.decide(record.task_id, request.request_id, True, "admin-user")
            claimed = runtime.queue.claim_next(runtime.worker_id)
            assert claimed is not None
        finally:
            runtime.shutdown()
        # The worker "dies" right after claiming the approved task.
        _recovery(stores).recover_worker(runtime.worker_id)
        assert _status(record.task_id) == TaskStatus.FAILED
        assert not (repo / "approved.py").exists()
        assert _queue(stores).claim_next(_register(stores, "w-new")) is None  # never re-queued

    async def test_approval_does_not_bypass_git_protection(self, stores, repo):
        self._coding_agent(repo, path=".git/config")
        runtime = WorkerRuntime(_runtime_config())
        runtime.startup()
        try:
            record = _submit(runtime.queue, "write git config")
            assert await self._execute_next(runtime) == TaskStatus.AWAITING_APPROVAL
            request = task_tracker.get_task(record.task_id).approvals[0]  # type: ignore[union-attr]
            approval_service.decide(record.task_id, request.request_id, True, "admin-user")
            assert await self._execute_next(runtime) == TaskStatus.FAILED
        finally:
            runtime.shutdown()
        assert (repo / ".git" / "config").read_text(encoding="utf-8") == "[core]\n"

    async def test_high_priority_plan_step_requires_approval(self, stores):
        bootstrap_scripted_agent()
        runtime = WorkerRuntime(_runtime_config())
        runtime.startup()
        try:
            record = _submit(runtime.queue, "privileged", priority=9)
            assert await self._execute_next(runtime) == TaskStatus.AWAITING_APPROVAL
            request = task_tracker.get_task(record.task_id).approvals[0]  # type: ignore[union-attr]
            assert request.action_type == "plan_step:1"
            approval_service.decide(record.task_id, request.request_id, True, "admin-user")
            assert await self._execute_next(runtime) == TaskStatus.COMPLETED
        finally:
            runtime.shutdown()

    def _in_progress_task(self, stores, user: str = ENG_A):
        queue = _queue(stores)
        record = _submit(queue, user=user)
        assert queue.claim_next(_register(stores, f"w-{uuid4().hex[:6]}")) is not None
        return record

    def _approve_pending(self, record, operation_args: dict) -> None:
        assert not approval_service.authorize_or_request(
            record.task_id,
            AgentId(id="coding-agent"),
            record.user_id,
            "tool:write_file",
            "local_write",
            operation_args,
            "test",
        )
        stored = task_tracker.get_task(record.task_id)

        def _force_approve(r):  # simulate the admin decision without the state check
            r.approvals[-1].approved = True

        task_tracker.store.update(stored.task_id, _force_approve)  # type: ignore[union-attr]

    def test_approval_is_bound_to_operation_task_and_engagement(self, stores):
        args = {"path": "a.py", "content": "1"}
        task_a = self._in_progress_task(stores)
        task_b = self._in_progress_task(stores)
        self._approve_pending(task_a, args)
        agent = AgentId(id="coding-agent")
        op = ("tool:write_file", "local_write")
        # Different arguments → not authorized
        assert not approval_service.authorize_or_request(task_a.task_id, agent, ENG_A, *op, {"path": "b.py"}, "t")
        # Same arguments, other task → not authorized
        assert not approval_service.authorize_or_request(task_b.task_id, agent, ENG_A, *op, args, "t")
        # Same task, wrong engagement → not authorized
        assert not approval_service.authorize_or_request(task_a.task_id, agent, ENG_B, *op, args, "t")
        # Exact match → authorized exactly once
        assert approval_service.authorize_or_request(task_a.task_id, agent, ENG_A, *op, args, "t")
        assert not approval_service.authorize_or_request(task_a.task_id, agent, ENG_A, *op, args, "t")

    def test_approval_cannot_be_decided_unless_awaiting(self, stores):
        record = self._in_progress_task(stores)
        approval_service.authorize_or_request(
            record.task_id, AgentId(id="coding-agent"), ENG_A, "tool:write_file", "local_write", {}, "t"
        )
        request = task_tracker.get_task(record.task_id).approvals[0]  # type: ignore[union-attr]
        with pytest.raises(InvalidStateTransitionError):
            approval_service.decide(record.task_id, request.request_id, True, "admin-user")

    def test_worker_code_has_no_path_to_grant_approval(self):
        sources = "".join(p.read_text(encoding="utf-8") for p in Path("baby/workers").glob("*.py"))
        assert "decide(" not in sources
        assert "approved = True" not in sources and "approved=True" not in sources


# ============================================================================
# 8. RECOVERY AND RESTARTS
# ============================================================================


class TestRecovery:
    def test_worker_dies_before_claiming(self, stores, clock):
        queue = _queue(stores, clock)
        dead = _register(stores, "w-dead", clock())
        record = _submit(queue)
        clock.advance(11)
        report = _recovery(stores, clock, stale=10).recover()
        assert report.stale_workers == [dead] and report.failed_tasks == []
        assert _status(record.task_id) == TaskStatus.PENDING
        claimed = queue.claim_next(_register(stores, "w-alive", clock()))
        assert claimed is not None and claimed.record.task_id == record.task_id

    def test_worker_dies_after_claiming(self, stores, clock):
        queue = _queue(stores, clock, lease=30)
        worker = _register(stores, "w-1", clock())
        record = _submit(queue)
        assert queue.claim_next(worker) is not None
        report = _recovery(stores, clock).recover_worker(worker, error="Worker process exited with code -9")
        assert [r.task_id for r in report.failed_tasks] == [record.task_id]
        stored = task_tracker.get_task(record.task_id)
        assert stored.status == TaskStatus.FAILED and "worker_exited" in stored.error  # type: ignore[union-attr]
        info = stores[1].get(worker)
        assert info.status == WorkerStatus.ERROR and info.tasks_failed == 1  # type: ignore[union-attr]
        types = [e.event_type for e in audit_log.get_events(task_id=record.task_id)]
        assert AuditEventType.TASK_RECOVERED in types and AuditEventType.TASK_FAILED in types

    def test_recovery_never_touches_awaiting_approval_or_terminal_tasks(self, stores, clock):
        queue = _queue(stores, clock)
        worker = _register(stores, "w-1", clock())
        _submit(queue, priority=9)
        claimed = queue.claim_next(worker)
        queue.park_for_approval(claimed.record.task_id, worker, claimed.lease.fence)  # type: ignore[union-attr]
        clock.advance(1000)
        _recovery(stores, clock).recover()
        assert _status(claimed.record.task_id) == TaskStatus.AWAITING_APPROVAL  # type: ignore[union-attr]

    async def test_api_restart_while_worker_alive_keeps_its_task(self, stores):
        queue = _queue(stores, lease=300)
        worker = _register(stores, "w-live")
        leased = _submit(queue, "leased")
        assert queue.claim_next(worker) is not None
        orphan = Task(title="orphan", description="in-process crash", user_id=ENG_A)
        task_tracker.register_task(orphan)
        task_tracker.update_status(orphan.id, TaskStatus.IN_PROGRESS)

        async with lifespan(app):
            assert _status(leased.task_id) == TaskStatus.IN_PROGRESS
            assert queue.get_lease(leased.task_id) is not None
            assert _status(orphan.id) == TaskStatus.FAILED
        # Stopping the API does not stop durable workers or take their claims away.
        assert stores[1].get(worker).status == WorkerStatus.BUSY  # type: ignore[union-attr]
        assert queue.get_lease(leased.task_id) is not None

    async def test_api_restart_fails_tasks_of_dead_workers(self, stores):
        queue = _queue(stores, lease=300)
        worker = _register(stores, "w-dead", datetime.utcnow() - timedelta(hours=1))
        record = _submit(queue)
        assert queue.claim_next(worker) is not None
        async with lifespan(app):
            stored = task_tracker.get_task(record.task_id)
            assert stored.status == TaskStatus.FAILED and "not replayed" in stored.error  # type: ignore

    def test_worker_restart_while_api_alive(self, stores, clock):
        queue = _queue(stores, clock)
        old = _register(stores, "w-old", clock())
        interrupted = _submit(queue, "interrupted")
        assert queue.claim_next(old) is not None
        fresh = _submit(queue, "fresh")
        clock.advance(31)  # old worker silent past stale timeout and lease
        replacement = WorkerRuntime(_runtime_config(stale_timeout=10, heartbeat_interval=60), clock=clock)
        replacement.startup()  # startup recovery runs before any claim
        try:
            assert _status(interrupted.task_id) == TaskStatus.FAILED
            claimed = replacement.queue.claim_next(replacement.worker_id)
            assert claimed is not None and claimed.record.task_id == fresh.task_id
            assert replacement.worker_id != old
        finally:
            replacement.shutdown()


# ============================================================================
# 9. API: SUBMISSION, STATUS, CONTROL, WORKER VISIBILITY
# ============================================================================


@pytest.fixture
def api():
    with patch.multiple(
        settings,
        api_require_auth=True,
        api_auth_token="op-token",
        api_admin_token="admin-token",
        operator_engagements=ENG_A,
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


OP = {"Authorization": "Bearer op-token"}
ADMIN = {"Authorization": "Bearer admin-token"}


class TestApi:
    def test_submit_returns_identity_and_never_executes(self, api):
        from baby.orchestration.orchestrator import orchestrator

        with (
            patch.object(orchestrator, "execute_task") as ex,
            patch.object(orchestrator, "execute_claimed_task") as claimed,
            patch.object(tool_executor, "execute") as tool,
        ):
            resp = api.post("/api/v1/tasks/submit", json={"title": "t", "description": "d"}, headers=OP)
            ex.assert_not_called()
            claimed.assert_not_called()
            tool.assert_not_called()
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "pending" and body["engagement_id"] == ENG_A
        status_resp = api.get(f"/tasks/{body['task_id']}", headers=OP)
        assert status_resp.status_code == 200 and status_resp.json()["status"] == "pending"

    def test_submit_is_available_on_both_mounts(self, api):
        for path in ("/tasks/submit", "/api/v1/tasks/submit"):
            assert api.post(path, json={"title": "t", "description": "d"}, headers=OP).status_code == 202

    def test_submit_requires_authentication(self, api):
        assert api.post("/tasks/submit", json={"title": "t", "description": "d"}).status_code == 401

    def test_submit_cannot_target_unauthorized_engagement(self, api):
        body = {"title": "t", "description": "d", "engagement_id": ENG_B}
        assert api.post("/tasks/submit", json=body, headers=OP).status_code == 403
        headers = {**OP, "X-Engagement-ID": ENG_B}
        assert api.post("/tasks/submit", json={"title": "t", "description": "d"}, headers=headers).status_code == 403
        assert task_tracker.count_tasks() == 0

    def test_submit_rejects_injected_fields_and_bad_bounds(self, api):
        injected = {"title": "t", "description": "d", "user_id": ENG_B, "status": "completed"}
        assert api.post("/tasks/submit", json=injected, headers=OP).status_code == 422
        assert (
            api.post("/tasks/submit", json={"title": "t", "description": "d", "priority": 11}, headers=OP).status_code
            == 422
        )
        assert api.post("/tasks/submit", json={"title": "", "description": "d"}, headers=OP).status_code == 422

    def test_read_only_guarantee_preserved_outside_task_control(self, api):
        assert api.post("/tasks", json={}, headers=ADMIN).status_code == 405
        assert api.put("/tasks/submit", json={}, headers=ADMIN).status_code == 405
        assert api.delete(f"/tasks/{uuid4()}/cancel", headers=ADMIN).status_code == 405
        assert api.post("/workers", json={}, headers=ADMIN).status_code == 405
        assert api.post(f"/tasks/{uuid4()}/approvals/not-a-uuid", json={}, headers=ADMIN).status_code == 405

    def test_cancel_scoped_to_engagement(self, api, stores):
        mine = _submit(_queue(stores), user=ENG_A)
        theirs = _submit(_queue(stores), user=ENG_B)
        assert api.post(f"/tasks/{theirs.task_id}/cancel", headers=OP).status_code == 404
        assert _status(theirs.task_id) == TaskStatus.PENDING
        resp = api.post(f"/tasks/{mine.task_id}/cancel", headers=OP)
        assert resp.status_code == 200 and resp.json()["status"] == "cancelled"
        assert api.post(f"/tasks/{mine.task_id}/cancel", headers=OP).status_code == 409

    def _parked(self, stores):
        queue = _queue(stores)
        worker = _register(stores, f"w-{uuid4().hex[:6]}")
        record = _submit(queue, priority=9)
        claimed = queue.claim_next(worker)
        approval_service.authorize_or_request(
            record.task_id, AgentId(id="orchestrator"), ENG_A, "plan_step:1", "plan_step_approval", {}, "t"
        )
        queue.park_for_approval(record.task_id, worker, claimed.lease.fence)  # type: ignore[union-attr]
        return record, task_tracker.get_task(record.task_id).approvals[0]  # type: ignore[union-attr]

    def test_only_admin_can_decide_approvals(self, api, stores):
        record, request = self._parked(stores)
        url = f"/api/v1/tasks/{record.task_id}/approvals/{request.request_id}"
        assert api.post(url, json={"approved": True}, headers=OP).status_code == 403
        assert api.post(url, json={"approved": True}).status_code == 401
        resp = api.post(url, json={"approved": True}, headers=ADMIN)
        assert resp.status_code == 200 and resp.json()["status"] == "awaiting_approval"
        assert api.post(url, json={"approved": True}, headers=ADMIN).status_code == 409
        decided = task_tracker.get_task(record.task_id).approvals[0]  # type: ignore[union-attr]
        assert decided.approved is True and decided.decided_by == "admin-user"

    def test_approval_request_must_belong_to_task(self, api, stores):
        record, _ = self._parked(stores)
        other, request = self._parked(stores)
        resp = api.post(
            f"/tasks/{record.task_id}/approvals/{request.request_id}", json={"approved": True}, headers=ADMIN
        )
        assert resp.status_code == 404

    def test_admin_denial_fails_task(self, api, stores):
        record, request = self._parked(stores)
        url = f"/tasks/{record.task_id}/approvals/{request.request_id}"
        assert api.post(url, json={"approved": False, "reason": "risky"}, headers=ADMIN).json()["status"] == "failed"

    def test_worker_status_visibility_is_scoped(self, api, stores):
        queue = _queue(stores)
        worker = _register(stores, "w-durable")
        theirs = _submit(queue, user=ENG_B)
        assert queue.claim_next(worker) is not None
        op_view = api.get("/workers/w-durable", headers=OP).json()
        assert op_view["status"] == "busy" and op_view["durable"] is True
        assert op_view["metadata"] == {} and op_view["current_task_id"] is None
        admin_view = api.get("/api/v1/workers/w-durable", headers=ADMIN).json()
        assert admin_view["metadata"]["hostname"] == "test-host"
        assert admin_view["current_task_id"]["id"] == str(theirs.task_id.id)
        listed = api.get("/workers?status=busy", headers=OP).json()
        assert [w["worker_id"] for w in listed] == ["w-durable"]

    def test_worker_events_visible_through_audit_api(self, api, stores):
        # The API lifespan attached the durable audit backend; a worker process writes to it too.
        SQLiteAuditLog(db_manager).record(
            AuditEventType.TASK_CLAIMED, user_id=ENG_A, details={"worker_id": "w-x", "fence": 1}
        )
        events = api.get("/audit?event_type=task_claimed", headers=OP).json()
        assert len(events) == 1 and events[0]["details"]["worker_id"] == "w-x"


# ============================================================================
# 10. SECURITY BOUNDARIES
# ============================================================================


class _FinancialTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            ToolSpec(
                name="place_order",
                description="Would place a live order",
                permissions_required=[
                    ToolPermission(category=PermissionCategory.FINANCIAL_ACTION, description="trade")
                ],
                input_schema={},
            )
        )
        self.called = False

    async def execute(self, **kwargs: Any) -> ToolResult:
        self.called = True
        return ToolResult(success=True)


class TestSecurity:
    def test_worker_cannot_write_results_for_task_it_does_not_own(self, stores, clock):
        queue = _queue(stores, clock)
        owner = _register(stores, "w-owner", clock())
        intruder = _register(stores, "w-intruder", clock())
        _submit(queue)
        claimed = queue.claim_next(owner)
        assert claimed is not None
        task_id, fence = claimed.record.task_id, claimed.lease.fence
        with pytest.raises(LeaseLostError):
            queue.finalize(task_id, intruder, fence, TaskStatus.COMPLETED)
        with pytest.raises(LeaseLostError):
            queue.finalize(task_id, owner, fence + 1, TaskStatus.COMPLETED)  # forged fence
        with pytest.raises(LeaseLostError):
            queue.park_for_approval(task_id, intruder, fence)
        assert queue.renew(task_id, intruder, fence).held is False
        assert _status(task_id) == TaskStatus.IN_PROGRESS

    def test_default_bootstrap_grants_only_declared_coding_permissions(self):
        default_bootstrap()
        default_bootstrap()  # idempotent
        perms = {p.category: p.level for p in permission_manager.get_agent_permissions("coding-agent")}
        assert perms == {
            PermissionCategory.LOCAL_READ: PermissionLevel.ALLOW,
            PermissionCategory.LOCAL_WRITE: PermissionLevel.APPROVAL_REQUIRED,
        }

    async def test_trading_execution_stays_blocked_in_workers(self):
        default_bootstrap()
        tool = _FinancialTool()
        with pytest.raises(PermissionDeniedError):
            await tool_executor.execute(tool, AgentId(id="coding-agent"))
        assert tool.called is False

    def test_task_ownership_does_not_replace_permission_checks(self, stores):
        # A task leased to a worker gives no tool permissions to an agent without grants.
        queue = _queue(stores)
        _submit(queue)
        assert queue.claim_next(_register(stores, "w-1")) is not None
        with pytest.raises(PermissionDeniedError):
            asyncio.run(tool_executor.execute(_FinancialTool(), AgentId(id="w-1")))

    def test_supervisor_refuses_to_run_inside_a_worker_process(self, monkeypatch):
        monkeypatch.setenv(WORKER_PROCESS_ENV, "1")
        with pytest.raises(RuntimeError, match="recursive"):
            WorkerSupervisor(worker_count=1).start()


# ============================================================================
# 11. CONFIGURATION
# ============================================================================


class TestWorkerConfiguration:
    def test_defaults_are_valid(self):
        cfg = Settings()
        validate_settings(cfg)
        assert cfg.worker_enabled is False and cfg.worker_count == 1

    @pytest.mark.parametrize(
        "field,value",
        [
            ("worker_count", 0),
            ("worker_count", 33),
            ("worker_count", 1.5),
            ("worker_poll_interval_seconds", 0),
            ("worker_heartbeat_interval_seconds", -1),
            ("worker_stale_timeout_seconds", 0),
            ("task_lease_duration_seconds", 10_000),
            ("worker_shutdown_grace_seconds", -1),
            ("worker_max_task_seconds", 0),
        ],
    )
    def test_out_of_bounds_values_raise(self, field, value):
        cfg = Settings()
        setattr(cfg, field, value)
        with pytest.raises(ConfigurationError):
            validate_settings(cfg)

    def test_stale_timeout_and_lease_must_cover_two_heartbeats(self):
        cfg = Settings()
        cfg.worker_heartbeat_interval_seconds = 10
        cfg.worker_stale_timeout_seconds = 15
        with pytest.raises(ConfigurationError, match="worker_stale_timeout_seconds"):
            validate_settings(cfg)
        cfg.worker_stale_timeout_seconds = 30
        cfg.task_lease_duration_seconds = 15
        with pytest.raises(ConfigurationError, match="task_lease_duration_seconds"):
            validate_settings(cfg)

    def test_workers_require_sqlite(self):
        cfg = Settings()
        cfg.worker_enabled = True
        cfg.persistence_backend = "in_memory"
        with pytest.raises(ConfigurationError):
            validate_settings(cfg)


# ============================================================================
# 12. REAL PROCESS ISOLATION (spawned worker processes)
# ============================================================================


@pytest.fixture
def supervisor_factory(durable_env):
    created = []

    def _make(**kwargs: Any) -> WorkerSupervisor:
        kwargs.setdefault("bootstrap", SCRIPTED_BOOTSTRAP)
        kwargs.setdefault("database_url", f"sqlite:///{durable_env}")
        kwargs.setdefault("settings_overrides", dict(FAST_WORKER_SETTINGS))
        sup = WorkerSupervisor(**kwargs)
        created.append(sup)
        return sup

    with patch.multiple(settings, **FAST_WORKER_SETTINGS):
        yield _make
    for sup in created:
        sup.stop(timeout=5)


class TestProcessIsolation:
    def test_workers_execute_in_separate_processes(self, supervisor_factory, stores):
        import os

        sup = supervisor_factory(worker_count=2)
        sup.start()
        ids = [_submit(_queue(stores), f"job-{i}").task_id for i in range(4)]
        assert _wait_for(lambda: all(_status(i) == TaskStatus.COMPLETED for i in ids))
        worker_ids = {h.worker_id for h in sup.workers}
        assert {task_tracker.get_task(i).assigned_worker_id for i in ids} <= worker_ids  # type: ignore[union-attr]
        assert _wait_for(lambda: all(stores[1].get(w) is not None for w in worker_ids))
        pids = {stores[1].get(w).metadata["pid"] for w in worker_ids}  # type: ignore[union-attr]
        assert os.getpid() not in pids and len(pids) == 2
        sup.stop(timeout=10)
        assert all(stores[1].get(w).status == WorkerStatus.STOPPED for w in worker_ids)  # type: ignore[union-attr]
        events = SQLiteAuditLog(db_manager).get_events(event_type=AuditEventType.WORKER_STARTED)
        assert len(events) == 2  # worker audit is durable and visible outside the worker

    def test_api_stays_responsive_while_worker_runs_long_task(self, supervisor_factory, api):
        from baby.orchestration.orchestrator import orchestrator

        sup = supervisor_factory(worker_count=1)
        sup.start()
        with patch.object(orchestrator, "execute_claimed_task", side_effect=AssertionError("API executed a task")):
            resp = api.post("/tasks/submit", json={"title": "long", "description": "sleep:3"}, headers=OP)
            task_id = resp.json()["task_id"]
            assert _wait_for(lambda: api.get(f"/tasks/{task_id}", headers=OP).json()["status"] == "in_progress")
            started = time.monotonic()
            for _ in range(10):
                assert api.get("/health").status_code == 200
                assert api.get(f"/tasks/{task_id}", headers=OP).json()["status"] == "in_progress"
            assert time.monotonic() - started < 2.0
            assert _wait_for(lambda: api.get(f"/tasks/{task_id}", headers=OP).json()["status"] == "completed")

    def test_killed_worker_fails_task_closed_and_is_replaced(self, supervisor_factory, stores):
        sup = supervisor_factory(worker_count=1, restart=True)
        sup.start()
        record = _submit(_queue(stores), "sleep:30")
        assert _wait_for(lambda: _status(record.task_id) == TaskStatus.IN_PROGRESS)
        (victim,) = sup.workers
        victim.process.kill()
        victim.process.join(timeout=10)
        assert sup.monitor_once() == [victim.worker_id]
        stored = task_tracker.get_task(record.task_id)
        assert stored.status == TaskStatus.FAILED and "not replayed" in stored.error  # type: ignore[union-attr]
        assert stores[1].get(victim.worker_id).status == WorkerStatus.ERROR  # type: ignore[union-attr]
        # A replacement with a new identity takes new work; the interrupted task is not replayed.
        follow_up = _submit(_queue(stores), "after-crash")
        assert _wait_for(lambda: _status(follow_up.task_id) == TaskStatus.COMPLETED)
        replacement = task_tracker.get_task(follow_up.task_id).assigned_worker_id  # type: ignore[union-attr]
        assert replacement != victim.worker_id and sup.restarts == 1
        assert _status(record.task_id) == TaskStatus.FAILED

    def test_graceful_shutdown_fails_in_flight_task_closed(self, supervisor_factory, stores):
        sup = supervisor_factory(worker_count=1)
        sup.start()
        record = _submit(_queue(stores), "sleep:30")
        assert _wait_for(lambda: _status(record.task_id) == TaskStatus.IN_PROGRESS)
        sup.stop(timeout=15)
        stored = task_tracker.get_task(record.task_id)
        assert stored.status == TaskStatus.FAILED and "shut down" in stored.error  # type: ignore[union-attr]
        (handle,) = sup.workers
        assert handle.process.exitcode == 0
        assert stores[1].get(handle.worker_id).status == WorkerStatus.STOPPED  # type: ignore[union-attr]

    def test_invalid_worker_configuration_fails_closed(self, supervisor_factory, stores):
        bad = dict(FAST_WORKER_SETTINGS, worker_stale_timeout_seconds=0.05)  # < 2 x heartbeat
        sup = supervisor_factory(worker_count=1, restart=True, settings_overrides=bad)
        sup.start()
        (handle,) = sup.workers
        handle.process.join(timeout=60)
        assert handle.process.exitcode == EXIT_CONFIG
        assert stores[1].list() == []  # never registered, never claimed
        sup.monitor_once()
        assert sup.restarts == 0 and len(sup.workers) == 1  # not restart-looped


# ============================================================================
# 13. IN-MEMORY TRACKER BACKEND (same state machine, no durable store)
# ============================================================================


class TestInMemoryTracker:
    @pytest.fixture
    def tracker(self):
        from baby.observability.tasks import TaskTracker

        with patch.object(settings, "persistence_backend", "in_memory"):
            tracker = TaskTracker()
        assert tracker.store is None
        return tracker

    def test_lifecycle_and_queries(self, tracker):
        t1 = Task(title="a", description="d", user_id=ENG_A)
        t2 = Task(title="b", description="d", user_id=ENG_B)
        tracker.register_task(t1)
        tracker.register_task(t2)
        tracker.update_status(t1.id, TaskStatus.IN_PROGRESS)
        assert tracker.get_task(t1.id).status == TaskStatus.IN_PROGRESS  # type: ignore[union-attr]
        assert [r.task_id for r in tracker.list_tasks(user_id=ENG_B)] == [t2.id]
        assert tracker.count_tasks(status=TaskStatus.PENDING) == 1
        assert tracker.list_tasks(limit=1, offset=1)[0].task_id in {t1.id, t2.id}
        with pytest.raises(InvalidStateTransitionError):
            tracker.update_status(t2.id, TaskStatus.COMPLETED)
        assert tracker.update_status(Task(title="x", description="y").id, TaskStatus.FAILED) is None

    def test_recovery_approvals_plan_and_verification(self, tracker):
        from baby.core import ApprovalRequest, Plan, PlanStep, VerificationResult

        task = Task(title="a", description="d", user_id=ENG_A)
        tracker.register_task(task)
        with pytest.raises(InvalidStateTransitionError):  # PENDING cannot await approval
            tracker.record_approval_request(
                task.id,
                ApprovalRequest(task_id=task.id, agent_id=AgentId(id="a"), action_type="x", reason="r", risk_level="h"),
            )
        tracker.update_status(task.id, TaskStatus.IN_PROGRESS)
        request = ApprovalRequest(
            task_id=task.id, agent_id=AgentId(id="a"), action_type="x", reason="r", risk_level="h"
        )
        assert tracker.record_approval_request(task.id, request).status == TaskStatus.AWAITING_APPROVAL  # type: ignore
        assert tracker.record_approval_decision(task.id, request.request_id, True).approvals[0].approved  # type: ignore
        tracker.set_plan(task.id, Plan(task_id=task.id, steps=[PlanStep(step_id=1, description="s")], reasoning="r"))
        tracker.record_verification(
            task.id, VerificationResult(agent_result_id=1, verified=True, verification_method="m")
        )
        tracker.update_status(task.id, TaskStatus.IN_PROGRESS)
        recovered = tracker.recover_stale_tasks()
        assert [r.task_id for r in recovered] == [task.id] and recovered[0].status == TaskStatus.FAILED
        record = tracker.get_task(task.id)
        assert record.plan is not None and len(record.verifications) == 1  # type: ignore[union-attr]
        tracker.clear()
        assert tracker.count_tasks() == 0
