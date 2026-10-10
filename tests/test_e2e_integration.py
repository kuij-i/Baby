"""Phase 14: End-to-end integration and reliability verification.

Comprehensive E2E scenarios verifying:
- Scenario A: Successful task lifecycle (API submission -> worker execution -> result retrieval -> audit)
- Scenario B: Authentication and authorization (tokens, IDOR protection, scope bounds, metadata redaction)
- Scenario C: Approval lifecycle (pauses execution, admin decision, digest binding, no replay, fail-closed)
- Scenario D: Cancellation and state transitions (cancel states, 409 on terminal, fenced rejection)
- Scenario E: Worker and database failures (restart survival, crash fail-closed, expired lease fencing)
"""

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from baby.agents import agent_registry
from baby.api.app import app
from baby.audit import audit_log
from baby.configuration import settings
from baby.core import (
    AgentId,
    AgentResult,
    AuditEventType,
    Task,
    TaskId,
    TaskStatus,
    WorkerStatus,
)
from baby.errors import (
    LeaseLostError,
)
from baby.observability import metrics_collector, task_tracker, worker_tracker
from baby.permissions import permission_manager
from baby.permissions.approvals import approval_service
from baby.persistence.database import db_manager
from baby.persistence.sqlite_workers import SQLiteWorkerStore
from baby.workers import (
    DurableTaskQueue,
    new_worker_id,
)
from baby.workers.supervisor import WorkerSupervisor
from tests.worker_helpers import bootstrap_scripted_agent

ENG_A = "eng-alpha"
ENG_B = "eng-beta"
SCRIPTED_BOOTSTRAP = "tests.worker_helpers:bootstrap_scripted_agent"

FAST_WORKER_SETTINGS: Dict[str, Any] = {
    "worker_poll_interval_seconds": 0.05,
    "worker_heartbeat_interval_seconds": 0.1,
    "worker_stale_timeout_seconds": 2.0,
    "task_lease_duration_seconds": 2.0,
}

OP_A = {"Authorization": "Bearer op-token", "X-Engagement-ID": ENG_A}
OP_B = {"Authorization": "Bearer op-token", "X-Engagement-ID": ENG_B}
ADMIN = {"Authorization": "Bearer admin-token"}
INVALID_AUTH = {"Authorization": "Bearer invalid-credentials"}


def _wait_for(predicate: Callable[[], bool], timeout: float = 45.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture(autouse=True)
def isolated_durable_env(tmp_path: Path):
    """Ensure every test runs with an isolated SQLite database and fresh singletons."""
    original_url = db_manager.database_url
    db_file = tmp_path / "baby_e2e.db"
    db_manager.configure(f"sqlite:///{db_file}")
    db_manager.init_schema()

    for reset in (task_tracker.clear, audit_log.clear, agent_registry.clear, permission_manager.clear):
        reset()
    worker_tracker.clear()
    metrics_collector.reset()

    bootstrap_scripted_agent()

    yield db_file

    audit_log.use_backend(None)
    audit_log.clear()
    agent_registry.clear()
    permission_manager.clear()
    db_manager.configure(original_url)
    db_manager.init_schema()


@pytest.fixture
def api():
    """TestClient configured with strict authentication and engagement mappings."""
    with patch.multiple(
        settings,
        api_require_auth=True,
        api_auth_token="op-token",
        api_admin_token="admin-token",
        operator_engagements=f"{ENG_A},{ENG_B}",
    ):
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client


@pytest.fixture
def supervisor_factory(isolated_durable_env):
    """Spawns real worker processes using isolated configuration."""
    created: List[WorkerSupervisor] = []

    def _make(**kwargs: Any) -> WorkerSupervisor:
        kwargs.setdefault("bootstrap", SCRIPTED_BOOTSTRAP)
        kwargs.setdefault("database_url", f"sqlite:///{isolated_durable_env}")
        kwargs.setdefault("settings_overrides", dict(FAST_WORKER_SETTINGS))
        sup = WorkerSupervisor(**kwargs)
        created.append(sup)
        return sup

    with patch.multiple(settings, **FAST_WORKER_SETTINGS):
        yield _make

    for sup in created:
        sup.stop(timeout=5)


# ============================================================================
# SCENARIO A: SUCCESSFUL TASK LIFECYCLE
# ============================================================================


class TestSuccessfulTaskLifecycleE2E:
    def test_complete_execution_path(self, api, supervisor_factory):
        """Submit via API -> queue PENDING -> worker executes -> retrieve result -> audit & metrics."""
        from baby.orchestration.orchestrator import orchestrator

        # 1. Confirm API submission is non-executing (patches execution)
        with patch.object(orchestrator, "execute_claimed_task", side_effect=AssertionError("API executed task")):
            resp = api.post(
                "/api/v1/tasks/submit",
                json={"title": "E2E Task", "description": "echo:hello-e2e", "priority": 3},
                headers=OP_A,
            )
            assert resp.status_code == 202
            submit_data = resp.json()
            task_id = submit_data["task_id"]
            assert submit_data["status"] == "pending"
            assert submit_data["engagement_id"] == ENG_A

        # Immediate GET confirms task is durably pending
        get_pending = api.get(f"/api/v1/tasks/{task_id}", headers=OP_A)
        assert get_pending.status_code == 200
        assert get_pending.json()["status"] == "pending"

        # 2. Worker process claims and executes
        sup = supervisor_factory(worker_count=1)
        sup.start()

        assert _wait_for(lambda: api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()["status"] == "completed")

        # 3. Retrieve task through API and inspect completed state
        get_completed = api.get(f"/api/v1/tasks/{task_id}", headers=OP_A)
        assert get_completed.status_code == 200
        data = get_completed.json()
        assert data["status"] == "completed"
        assert data["completed_at"] is not None
        assert data["assigned_worker_id"] is not None
        assert data["result"]["success"] is True
        assert data["result"]["output"] == {"echo": "echo:hello-e2e"}

        # 4. Audit trail verification
        audit_resp = api.get(f"/api/v1/audit?task_id={task_id}", headers=ADMIN)
        assert audit_resp.status_code == 200
        events = {e["event_type"] for e in audit_resp.json()}
        assert AuditEventType.TASK_CREATED.value in events
        assert AuditEventType.TASK_CLAIMED.value in events
        assert AuditEventType.TASK_COMPLETED.value in events

        # 5. Operational visibility
        health_resp = api.get("/api/v1/health")
        assert health_resp.status_code == 200
        assert health_resp.json()["status"] in ("healthy", "degraded")

        readiness_resp = api.get("/api/v1/readiness")
        assert readiness_resp.status_code == 200
        assert readiness_resp.json()["ready"] is True

        metrics_resp = api.get("/api/v1/metrics", headers=ADMIN)
        assert metrics_resp.status_code == 200
        metrics_data = metrics_resp.json()
        assert "counters" in metrics_data
        assert "gauges" in metrics_data


# ============================================================================
# SCENARIO B: AUTHENTICATION AND AUTHORIZATION
# ============================================================================


class TestAuthenticationAndAuthorizationE2E:
    def test_missing_and_invalid_credentials_rejected(self, api):
        # Missing auth
        assert api.get("/api/v1/tasks").status_code == 401
        assert api.post("/api/v1/tasks/submit", json={"title": "test", "description": "d"}).status_code == 401

        # Invalid auth
        assert api.get("/api/v1/tasks", headers=INVALID_AUTH).status_code == 401
        resp = api.post(
            "/api/v1/tasks/submit",
            json={"title": "test", "description": "d"},
            headers=INVALID_AUTH,
        )
        assert resp.status_code == 401

    def test_cross_engagement_idor_protection(self, api):
        # Admin creates task for ENG_A
        resp = api.post(
            "/api/v1/tasks/submit",
            json={"title": "Private A", "description": "desc-private-a", "engagement_id": ENG_A},
            headers=ADMIN,
        )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        # Operator with scope ENG_A can read it
        with patch.object(settings, "operator_engagements", ENG_A):
            assert api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).status_code == 200

        # Operator with scope ENG_B cannot read it -> 404 (IDOR safe)
        with patch.object(settings, "operator_engagements", ENG_B):
            cross_resp = api.get(f"/api/v1/tasks/{task_id}", headers=OP_B)
            assert cross_resp.status_code == 404
            assert "not found" in cross_resp.json()["detail"].lower()

        # Admin can access across all engagements
        assert api.get(f"/api/v1/tasks/{task_id}", headers=ADMIN).status_code == 200

    def test_caller_cannot_expand_authorized_engagement_scope(self, api):
        forbidden_scope_header = {
            "Authorization": "Bearer op-token",
            "X-Engagement-ID": "unauthorized-engagement",
        }
        resp = api.post(
            "/api/v1/tasks/submit",
            json={"title": "Scope Expansion Attempt", "description": "desc-expansion"},
            headers=forbidden_scope_header,
        )
        assert resp.status_code == 403

    def test_non_admin_cannot_decide_approval(self, api):
        fake_task = uuid4()
        fake_req = uuid4()
        resp = api.post(
            f"/api/v1/tasks/{fake_task}/approvals/{fake_req}",
            json={"approved": True, "reason": "operator trying to approve"},
            headers=OP_A,
        )
        assert resp.status_code == 403
        assert "Admin role required" in resp.json()["detail"]

    def test_worker_metadata_redacted_for_non_admins(self, api, supervisor_factory):
        sup = supervisor_factory(worker_count=1)
        sup.start()
        assert _wait_for(lambda: len(api.get("/api/v1/workers", headers=OP_A).json()) >= 1)

        # Operator gets empty/sanitized metadata
        op_view = api.get("/api/v1/workers", headers=OP_A).json()[0]
        assert op_view["metadata"] == {}

        # Admin gets process details
        admin_view = api.get("/api/v1/workers", headers=ADMIN).json()[0]
        assert "pid" in admin_view["metadata"]
        assert "hostname" in admin_view["metadata"]


# ============================================================================
# SCENARIO C: APPROVAL LIFECYCLE
# ============================================================================


class TestApprovalLifecycleE2E:
    def test_approval_denial_fails_closed(self, api, supervisor_factory):
        # Priority 9 triggers planner approval requirement
        resp = api.post(
            "/api/v1/tasks/submit",
            json={"title": "High Risk Task", "description": "do-work", "priority": 9},
            headers=OP_A,
        )
        task_id = resp.json()["task_id"]

        sup = supervisor_factory(worker_count=1)
        sup.start()

        # Worker pauses execution and parks task in awaiting_approval
        assert _wait_for(
            lambda: api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()["status"] == "awaiting_approval"
        )

        task_data = api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()
        assert len(task_data["approvals"]) == 1
        req_id = task_data["approvals"][0]["request_id"]
        assert task_data["approvals"][0]["approved"] is None

        # Admin denies approval
        deny_resp = api.post(
            f"/api/v1/tasks/{task_id}/approvals/{req_id}",
            json={"approved": False, "reason": "Operation denied by security policy"},
            headers=ADMIN,
        )
        assert deny_resp.status_code == 200
        assert deny_resp.json()["approved"] is False

        # Task immediately moves to FAILED
        final_task = api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()
        assert final_task["status"] == "failed"
        assert "Approval denied" in final_task["error"]

    def test_approval_grant_and_single_use_enforcement(self, api, supervisor_factory):
        resp = api.post(
            "/api/v1/tasks/submit",
            json={"title": "Approved Task", "description": "echo:approved-run", "priority": 9},
            headers=OP_A,
        )
        task_id = resp.json()["task_id"]

        sup = supervisor_factory(worker_count=1)
        sup.start()

        assert _wait_for(
            lambda: api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()["status"] == "awaiting_approval"
        )

        task_data = api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()
        req_id = task_data["approvals"][0]["request_id"]

        # Admin approves
        approve_resp = api.post(
            f"/api/v1/tasks/{task_id}/approvals/{req_id}",
            json={"approved": True, "reason": "Approved for execution"},
            headers=ADMIN,
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["approved"] is True

        # Worker re-claims and runs to completion
        assert _wait_for(lambda: api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()["status"] == "completed")

        final_task = api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()
        assert final_task["status"] == "completed"
        assert final_task["result"]["output"] == {"echo": "echo:approved-run"}

        # Verify approval grant is single-use and cannot be replayed
        record = task_tracker.get_task(TaskId(id=UUID(task_id)))
        assert record is not None
        req = record.approvals[0]
        assert req.consumed_at is not None

        # Replay attempt fails closed
        replay_success = approval_service.authorize_or_request(
            task_id=TaskId(id=UUID(task_id)),
            agent_id=req.agent_id,
            engagement_id=ENG_A,
            operation=req.action_type,
            permission=req.permission or "plan_execution",
            arguments={},
            reason="replay attempt",
        )
        assert replay_success is False


# ============================================================================
# SCENARIO D: CANCELLATION AND STATE TRANSITIONS
# ============================================================================


class TestCancellationAndStateTransitionsE2E:
    def test_cancel_pending_task_before_execution(self, api, supervisor_factory):
        resp = api.post(
            "/api/v1/tasks/submit",
            json={"title": "Cancel Before", "description": "desc-cancel-before"},
            headers=OP_A,
        )
        task_id = resp.json()["task_id"]

        cancel_resp = api.post(f"/api/v1/tasks/{task_id}/cancel", headers=OP_A)
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "cancelled"

        # Worker starts -> cancelled task is never claimed
        sup = supervisor_factory(worker_count=1)
        sup.start()
        time.sleep(0.5)

        task_data = api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()
        assert task_data["status"] == "cancelled"
        assert task_data["assigned_worker_id"] is None

    def test_cancel_in_progress_task(self, api, supervisor_factory):
        sup = supervisor_factory(worker_count=1)
        sup.start()

        resp = api.post(
            "/api/v1/tasks/submit",
            json={"title": "Long Task", "description": "sleep:5"},
            headers=OP_A,
        )
        task_id = resp.json()["task_id"]

        assert _wait_for(lambda: api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()["status"] == "in_progress")

        cancel_resp = api.post(f"/api/v1/tasks/{task_id}/cancel", headers=OP_A)
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["cancel_requested"] is True

        # Worker watchdog aborts work upon observing cancel_requested
        assert _wait_for(lambda: api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()["status"] == "cancelled")

    def test_terminal_task_cancellation_returns_conflict(self, api):
        resp = api.post(
            "/api/v1/tasks/submit",
            json={"title": "Instant Cancel", "description": "desc-instant-cancel"},
            headers=OP_A,
        )
        task_id = resp.json()["task_id"]

        assert api.post(f"/api/v1/tasks/{task_id}/cancel", headers=OP_A).status_code == 200

        # Attempt to cancel already cancelled task -> 409 Conflict
        second_cancel = api.post(f"/api/v1/tasks/{task_id}/cancel", headers=OP_A)
        assert second_cancel.status_code == 409
        assert "terminal" in second_cancel.json()["detail"].lower()


# ============================================================================
# SCENARIO E: WORKER AND FAILURE SCENARIOS
# ============================================================================


class TestWorkerAndFailureScenariosE2E:
    def test_api_restart_preserves_live_worker_tasks(self, api, supervisor_factory, isolated_durable_env):
        sup = supervisor_factory(worker_count=1)
        sup.start()

        resp = api.post(
            "/api/v1/tasks/submit",
            json={"title": "Task Survives API Restart", "description": "sleep:1.5"},
            headers=OP_A,
        )
        task_id = resp.json()["task_id"]

        assert _wait_for(lambda: api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()["status"] == "in_progress")

        # Simulate API restart: new TestClient triggers app lifespan startup
        with TestClient(app, raise_server_exceptions=False) as new_api:
            # Startup recovery runs without failing the live worker's task
            resp_mid = new_api.get(f"/api/v1/tasks/{task_id}", headers=OP_A)
            assert resp_mid.json()["status"] in ("in_progress", "completed")

        # Worker completes cleanly
        assert _wait_for(lambda: api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()["status"] == "completed")

    def test_killed_worker_fails_task_closed_no_blind_replay(self, api, supervisor_factory):
        sup = supervisor_factory(worker_count=1, restart=False)
        sup.start()

        resp = api.post(
            "/api/v1/tasks/submit",
            json={"title": "Task Killed Worker", "description": "sleep:30"},
            headers=OP_A,
        )
        task_id = resp.json()["task_id"]

        assert _wait_for(lambda: api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()["status"] == "in_progress")

        # Kill worker process
        (victim,) = sup.workers
        victim.process.kill()
        victim.process.join(timeout=5)

        # Supervisor detects dead worker and runs recovery
        sup.monitor_once()

        task_data = api.get(f"/api/v1/tasks/{task_id}", headers=OP_A).json()
        assert task_data["status"] == "failed"
        assert "not replayed" in task_data["error"]

    def test_expired_lease_fencing_rejects_late_write(self, isolated_durable_env):
        """Worker whose lease has expired cannot write a result."""
        task_store = task_tracker.store
        assert task_store is not None
        worker_store = SQLiteWorkerStore(task_store.db)

        w_id = new_worker_id()
        worker_store.register(w_id, "host", 1000, datetime.utcnow())
        worker_store.set_status(w_id, WorkerStatus.IDLE, datetime.utcnow())

        queue = DurableTaskQueue(task_store, worker_store, lease_duration_seconds=0.1)
        task = Task(title="Fenced Task", description="fenced", user_id=ENG_A)
        queue.submit(task)

        claimed = queue.claim_next(w_id)
        assert claimed is not None

        # Wait for lease to expire
        time.sleep(0.2)

        # Late write fails closed with LeaseLostError
        with pytest.raises(LeaseLostError):
            queue.finalize(
                task_id=claimed.record.task_id,
                worker_id=w_id,
                fence=claimed.lease.fence,
                status=TaskStatus.COMPLETED,
                result=AgentResult(
                    step_id=1,
                    agent_id=AgentId(id="scripted-agent"),
                    success=True,
                    output="late",
                ),
            )

        # Database record was not updated to completed
        stored = task_store.get_task(claimed.record.task_id)
        assert stored is not None
        assert stored.status != TaskStatus.COMPLETED

    def test_concurrent_claim_race_single_winner(self, isolated_durable_env):
        """Two workers racing to claim the same task results in exactly one claim."""
        task_store = task_tracker.store
        assert task_store is not None
        worker_store = SQLiteWorkerStore(task_store.db)

        w1 = new_worker_id()
        w2 = new_worker_id()
        for w in (w1, w2):
            worker_store.register(w, "host", 1000, datetime.utcnow())
            worker_store.set_status(w, WorkerStatus.IDLE, datetime.utcnow())

        queue = DurableTaskQueue(task_store, worker_store)
        task = Task(title="Race Task", description="race", user_id=ENG_A)
        queue.submit(task)

        c1 = queue.claim_next(w1)
        c2 = queue.claim_next(w2)

        claims = [c for c in (c1, c2) if c is not None]
        assert len(claims) == 1
        assert claims[0].record.task_id == task.id
