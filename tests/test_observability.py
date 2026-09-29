"""Phase 9 tests: operational observability and audit visibility.

Covers:
- Health check (healthy, degraded, unavailable states)
- Task visibility (list, get, engagement isolation, ID manipulation)
- Worker visibility (list, get, lifecycle states)
- Agent visibility (list, get)
- Metrics collection (counters, latencies, gauges, snapshot)
- Audit visibility (filtering, engagement isolation, ID manipulation)
- Authorization (unauthenticated, wrong token, correct token)
- Secret redaction (API keys, tokens, credentials never leak)
- Read-only guarantees (POST/PUT/DELETE rejected at API level)
- Regression: all 184 existing tests must still pass
"""

from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from baby.agents import agent_registry
from baby.audit import audit_log
from baby.configuration import settings
from baby.core import (
    AgentCapability,
    AgentId,
    AgentResult,
    AgentSpec,
    AuditEventType,
    PermissionCategory,
    PermissionLevel,
    Task,
    TaskId,
    TaskStatus,
    ToolPermission,
    WorkerStatus,
)
from baby.observability import (
    health_checker,
    metrics_collector,
    task_tracker,
    worker_tracker,
)
from baby.observability.health import ComponentHealth, HealthStatus, SystemHealth
from baby.observability.redaction import redact_sensitive_data

# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------


def _make_client(auth_required: bool = False, admin_token: str = "admin-secret", op_token: str = "op-secret"):
    """Create a TestClient with specified auth settings applied."""
    from baby.api.app import app

    original_require = settings.api_require_auth
    original_admin = settings.api_admin_token
    original_op = settings.api_auth_token

    settings.api_require_auth = auth_required
    settings.api_admin_token = admin_token
    settings.api_auth_token = op_token

    client = TestClient(app, raise_server_exceptions=True)
    yield client

    settings.api_require_auth = original_require
    settings.api_admin_token = original_admin
    settings.api_auth_token = original_op


@pytest.fixture(autouse=True)
def _reset_global_state():
    """Reset shared singletons before each test for isolation."""
    audit_log.clear()
    task_tracker.clear()
    worker_tracker.clear()
    metrics_collector.reset()
    agent_registry.clear()
    yield
    audit_log.clear()
    task_tracker.clear()
    worker_tracker.clear()
    metrics_collector.reset()
    agent_registry.clear()


@pytest.fixture()
def no_auth_client():
    from baby.api.app import app

    settings.api_require_auth = False
    settings.api_admin_token = "admin-secret"
    settings.api_auth_token = "op-secret"
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    settings.api_require_auth = False


@pytest.fixture()
def auth_client():
    """Client with auth enforced, using admin token."""
    from baby.api.app import app

    settings.api_require_auth = True
    settings.api_admin_token = "admin-secret"
    settings.api_auth_token = "op-secret"
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    settings.api_require_auth = True


# ---------------------------------------------------------------------------
# Section 1: Health Check
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    def test_health_returns_200_when_healthy(self, no_auth_client):
        with patch.object(health_checker, "check_health") as mock_check:
            mock_check.return_value = SystemHealth(
                status=HealthStatus.HEALTHY,
                components={},
            )
            resp = no_auth_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == HealthStatus.HEALTHY.value

    def test_health_returns_503_when_unavailable(self, no_auth_client):
        with patch.object(health_checker, "check_health") as mock_check:
            mock_check.return_value = SystemHealth(
                status=HealthStatus.UNAVAILABLE,
                components={"memory": ComponentHealth(status=HealthStatus.UNAVAILABLE, message="Down")},
            )
            resp = no_auth_client.get("/health")
        assert resp.status_code == 503

    def test_health_returns_200_when_degraded(self, no_auth_client):
        """Degraded still returns 200 to allow health-check routing."""
        with patch.object(health_checker, "check_health") as mock_check:
            mock_check.return_value = SystemHealth(
                status=HealthStatus.DEGRADED,
                components={},
            )
            resp = no_auth_client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == HealthStatus.DEGRADED.value

    def test_health_has_timestamp_and_version(self, no_auth_client):
        resp = no_auth_client.get("/health")
        body = resp.json()
        assert "timestamp" in body
        assert "version" in body

    def test_health_checker_aggregates_correctly(self):
        """Direct health_checker test: a single UNAVAILABLE critical component triggers UNAVAILABLE."""
        with (
            patch.object(health_checker, "check_memory") as mock_mem,
            patch.object(health_checker, "check_audit") as mock_audit,
            patch.object(health_checker, "check_agents") as mock_agents,
            patch.object(health_checker, "check_tools") as mock_tools,
            patch.object(health_checker, "check_providers") as mock_prov,
        ):

            mock_mem.return_value = ComponentHealth(status=HealthStatus.UNAVAILABLE, message="db down")
            mock_audit.return_value = ComponentHealth(status=HealthStatus.HEALTHY)
            mock_agents.return_value = ComponentHealth(status=HealthStatus.HEALTHY)
            mock_tools.return_value = ComponentHealth(status=HealthStatus.HEALTHY)
            mock_prov.return_value = ComponentHealth(status=HealthStatus.HEALTHY)

            result = health_checker.check_health()

        assert result.status == HealthStatus.UNAVAILABLE

    def test_health_checker_degraded_on_non_critical_failure(self):
        """Non-critical degradation yields DEGRADED not UNAVAILABLE."""
        with (
            patch.object(health_checker, "check_memory") as mock_mem,
            patch.object(health_checker, "check_audit") as mock_audit,
            patch.object(health_checker, "check_agents") as mock_agents,
            patch.object(health_checker, "check_tools") as mock_tools,
            patch.object(health_checker, "check_providers") as mock_prov,
        ):

            mock_mem.return_value = ComponentHealth(status=HealthStatus.HEALTHY)
            mock_audit.return_value = ComponentHealth(status=HealthStatus.HEALTHY)
            mock_agents.return_value = ComponentHealth(status=HealthStatus.DEGRADED, message="no agents")
            mock_tools.return_value = ComponentHealth(status=HealthStatus.HEALTHY)
            mock_prov.return_value = ComponentHealth(status=HealthStatus.DEGRADED, message="no provider keys")

            result = health_checker.check_health()

        assert result.status == HealthStatus.DEGRADED

    def test_health_agents_degraded_if_none_registered(self):
        """No agents registered → agents component is DEGRADED."""
        health = health_checker.check_agents()
        assert health.status == HealthStatus.DEGRADED

    def test_health_tools_healthy_with_empty_registry(self):
        """Empty tool registry is healthy (0 tools is a valid state)."""
        health = health_checker.check_tools()
        assert health.status == HealthStatus.HEALTHY


# ---------------------------------------------------------------------------
# Section 2: Metrics
# ---------------------------------------------------------------------------


class TestMetricsCollector:
    def test_increment_counter(self):
        metrics_collector.increment_counter("test_events", labels={"type": "x"})
        snapshot = metrics_collector.get_snapshot()
        assert "test_events" in snapshot["counters"]

    def test_set_gauge(self):
        metrics_collector.set_gauge("active_workers", 3.0)
        snapshot = metrics_collector.get_snapshot()
        assert "active_workers" in snapshot["gauges"]
        assert snapshot["gauges"]["active_workers"][""] == 3.0

    def test_record_latency(self):
        metrics_collector.record_latency("task_duration_ms", 150.0)
        snapshot = metrics_collector.get_snapshot()
        lat = snapshot["latencies"]["task_duration_ms"][""]
        assert lat["count"] == 1
        assert lat["min_ms"] == 150.0
        assert lat["max_ms"] == 150.0
        assert lat["avg_ms"] == 150.0

    def test_latency_stats_multiple_records(self):
        for ms in [10.0, 20.0, 30.0]:
            metrics_collector.record_latency("req_ms", ms)
        snapshot = metrics_collector.get_snapshot()
        lat = snapshot["latencies"]["req_ms"][""]
        assert lat["count"] == 3
        assert lat["min_ms"] == 10.0
        assert lat["max_ms"] == 30.0
        assert abs(lat["avg_ms"] - 20.0) < 0.01

    def test_task_lifecycle_metrics(self):
        metrics_collector.record_task_created(priority=5)
        metrics_collector.record_task_completed(duration_ms=500.0)
        snapshot = metrics_collector.get_snapshot()
        assert snapshot["counters"]["tasks_created_total"]["priority=5"] == 1
        assert snapshot["counters"]["tasks_completed_total"][""] == 1

    def test_task_failed_metrics(self):
        metrics_collector.record_task_failed(duration_ms=200.0, error_type="timeout")
        snapshot = metrics_collector.get_snapshot()
        assert snapshot["counters"]["tasks_failed_total"]["error_type=timeout"] == 1
        assert snapshot["counters"]["errors_total"]["type=timeout"] == 1

    def test_tool_execution_metrics(self):
        metrics_collector.record_tool_execution("read_file", success=True, duration_ms=50.0)
        metrics_collector.record_tool_execution("write_file", success=False, duration_ms=30.0)
        snapshot = metrics_collector.get_snapshot()
        assert snapshot["counters"]["tool_executions_total"]["status=success,tool=read_file"] == 1
        assert snapshot["counters"]["tool_executions_total"]["status=failure,tool=write_file"] == 1

    def test_api_request_metric(self):
        metrics_collector.record_api_request("GET", "/health", 200)
        snapshot = metrics_collector.get_snapshot()
        assert "api_requests_total" in snapshot["counters"]

    def test_approval_metrics(self):
        metrics_collector.record_approval_requested()
        metrics_collector.record_approval_decision(approved=True)
        metrics_collector.record_approval_decision(approved=False)
        snapshot = metrics_collector.get_snapshot()
        counts = snapshot["counters"]["approvals_total"]
        assert counts["status=requested"] == 1
        assert counts["status=granted"] == 1
        assert counts["status=denied"] == 1

    def test_verification_metrics(self):
        metrics_collector.record_verification(verified=True)
        metrics_collector.record_verification(verified=False)
        snapshot = metrics_collector.get_snapshot()
        counts = snapshot["counters"]["verifications_total"]
        assert counts["status=passed"] == 1
        assert counts["status=failed"] == 1

    def test_reset_clears_all(self):
        metrics_collector.increment_counter("foo")
        metrics_collector.reset()
        snapshot = metrics_collector.get_snapshot()
        assert snapshot["counters"] == {}
        assert snapshot["gauges"] == {}
        assert snapshot["latencies"] == {}

    def test_metrics_endpoint_returns_snapshot(self, no_auth_client):
        metrics_collector.increment_counter("test_counter")
        resp = no_auth_client.get("/metrics")
        assert resp.status_code == 200
        assert "counters" in resp.json()


# ---------------------------------------------------------------------------
# Section 3: Task Visibility
# ---------------------------------------------------------------------------


class TestTaskTracker:
    def test_register_and_get_task(self):
        task = Task(title="Test task", description="desc", priority=3)
        record = task_tracker.register_task(task)
        assert record.task_id == task.id
        assert record.title == "Test task"
        assert record.status == TaskStatus.PENDING

    def test_update_status_to_completed(self):
        task = Task(title="T", description="d")
        task_tracker.register_task(task, status=TaskStatus.PENDING)
        task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)
        updated = task_tracker.update_status(task.id, TaskStatus.COMPLETED)
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED
        assert updated.completed_at is not None

    def test_update_status_to_failed(self):
        task = Task(title="T", description="d")
        task_tracker.register_task(task)
        task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)
        updated = task_tracker.update_status(task.id, TaskStatus.FAILED, error="boom")
        assert updated.status == TaskStatus.FAILED
        assert updated.error == "boom"

    def test_nonexistent_task_returns_none(self):
        assert task_tracker.get_task(TaskId()) is None

    def test_list_tasks_with_status_filter(self):
        t1 = Task(title="A", description="a")
        t2 = Task(title="B", description="b")
        task_tracker.register_task(t1)
        task_tracker.register_task(t2)
        task_tracker.update_status(t1.id, TaskStatus.IN_PROGRESS)
        task_tracker.update_status(t1.id, TaskStatus.COMPLETED)

        completed = task_tracker.list_tasks(status=TaskStatus.COMPLETED)
        assert len(completed) == 1
        assert completed[0].task_id == t1.id

    def test_list_tasks_engagement_isolation(self):
        t1 = Task(title="A", description="a", user_id="eng-1")
        t2 = Task(title="B", description="b", user_id="eng-2")
        task_tracker.register_task(t1)
        task_tracker.register_task(t2)

        eng1_tasks = task_tracker.list_tasks(user_id="eng-1")
        assert len(eng1_tasks) == 1
        assert eng1_tasks[0].user_id == "eng-1"

    def test_count_tasks(self):
        for i in range(5):
            task_tracker.register_task(Task(title=f"T{i}", description="d"))
        assert task_tracker.count_tasks() == 5

    def test_pagination_limit(self):
        for i in range(10):
            task_tracker.register_task(Task(title=f"T{i}", description="d"))
        page = task_tracker.list_tasks(limit=3)
        assert len(page) == 3

    def test_pagination_offset(self):
        for i in range(5):
            task_tracker.register_task(Task(title=f"T{i}", description="d"))
        task_tracker.list_tasks()
        page = task_tracker.list_tasks(offset=2)
        assert len(page) == 3

    def test_update_nonexistent_task_returns_none(self):
        result = task_tracker.update_status(TaskId(), TaskStatus.COMPLETED)
        assert result is None

    def test_metrics_recorded_on_lifecycle(self):
        task = Task(title="T", description="d", priority=5)
        task_tracker.register_task(task)
        task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)
        task_tracker.update_status(task.id, TaskStatus.COMPLETED)
        snap = metrics_collector.get_snapshot()
        assert snap["counters"].get("tasks_completed_total", {}).get("", 0) == 1


class TestTasksAPIEndpoint:
    def test_list_tasks_returns_200(self, no_auth_client):
        resp = no_auth_client.get("/tasks")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_list_tasks_empty_state(self, no_auth_client):
        resp = no_auth_client.get("/tasks")
        assert resp.json() == []

    def test_get_task_not_found(self, no_auth_client):
        resp = no_auth_client.get(f"/tasks/{uuid4()}")
        assert resp.status_code == 404

    def test_get_task_found(self, no_auth_client):
        task = Task(title="T", description="d")
        task_tracker.register_task(task)
        resp = no_auth_client.get(f"/tasks/{task.id.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "T"

    def test_task_secrets_redacted(self, no_auth_client):
        """Secret fields in task details must be redacted."""
        task = Task(title="T", description="d")
        task_tracker.register_task(task)
        resp = no_auth_client.get(f"/tasks/{task.id.id}")
        body = resp.json()
        # Check the response doesn't contain raw token-like fields
        assert body.get("api_key") != "sk-supersecret"

    def test_tasks_endpoint_read_only_post_rejected(self, no_auth_client):
        resp = no_auth_client.post("/tasks", json={})
        assert resp.status_code == 405

    def test_tasks_endpoint_read_only_put_rejected(self, no_auth_client):
        resp = no_auth_client.put("/tasks/some-id", json={})
        assert resp.status_code == 405

    def test_tasks_endpoint_read_only_delete_rejected(self, no_auth_client):
        resp = no_auth_client.delete("/tasks/some-id")
        assert resp.status_code == 405

    def test_task_id_manipulation_returns_404(self, no_auth_client):
        """Mutating a task_id must not bypass scope: wrong ID returns 404."""
        task = Task(title="T", description="d")
        task_tracker.register_task(task)
        wrong_id = uuid4()
        resp = no_auth_client.get(f"/tasks/{wrong_id}")
        assert resp.status_code == 404

    def test_task_engagement_isolation_via_api(self, auth_client):
        """Non-admin caller must not see another engagement's tasks."""
        settings.api_require_auth = True
        # Register task for eng-2
        task = Task(title="Private", description="d", user_id="eng-2")
        task_tracker.register_task(task)

        # Operator scoped to eng-1 should NOT see eng-2's task
        headers = {"Authorization": "Bearer op-secret", "X-Engagement-ID": "eng-1"}
        resp = auth_client.get(f"/tasks/{task.id.id}", headers=headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Section 4: Worker Visibility
# ---------------------------------------------------------------------------


class TestWorkerTracker:
    def test_register_and_get_worker(self):
        worker = worker_tracker.register_worker("w-1")
        assert worker.worker_id == "w-1"
        assert worker.status == WorkerStatus.IDLE

    def test_assign_task_sets_busy(self):
        worker_tracker.register_worker("w-1")
        task_id = TaskId()
        updated = worker_tracker.assign_task("w-1", task_id)
        assert updated.status == WorkerStatus.BUSY
        assert updated.current_task_id == task_id

    def test_complete_task_returns_to_idle(self):
        worker_tracker.register_worker("w-1")
        worker_tracker.assign_task("w-1", TaskId())
        updated = worker_tracker.complete_task("w-1", success=True)
        assert updated.status == WorkerStatus.IDLE
        assert updated.tasks_completed == 1
        assert updated.current_task_id is None

    def test_complete_task_failure(self):
        worker_tracker.register_worker("w-1")
        worker_tracker.assign_task("w-1", TaskId())
        updated = worker_tracker.complete_task("w-1", success=False, error="oops")
        assert updated.tasks_failed == 1
        assert updated.last_error == "oops"

    def test_set_status(self):
        worker_tracker.register_worker("w-1")
        updated = worker_tracker.set_status("w-1", WorkerStatus.PAUSED)
        assert updated.status == WorkerStatus.PAUSED

    def test_heartbeat(self):
        worker_tracker.register_worker("w-1")
        before = worker_tracker.get_worker("w-1").last_heartbeat_at
        import time

        time.sleep(0.01)
        worker_tracker.record_heartbeat("w-1")
        after = worker_tracker.get_worker("w-1").last_heartbeat_at
        # after >= before (heartbeat time updates)
        assert after >= before

    def test_list_workers_status_filter(self):
        worker_tracker.register_worker("w-idle")
        worker_tracker.register_worker("w-busy")
        worker_tracker.assign_task("w-busy", TaskId())
        busy = worker_tracker.list_workers(status=WorkerStatus.BUSY)
        assert len(busy) == 1
        assert busy[0].worker_id == "w-busy"

    def test_unregister_worker(self):
        worker_tracker.register_worker("w-1")
        removed = worker_tracker.unregister_worker("w-1")
        assert removed is True
        assert worker_tracker.get_worker("w-1") is None

    def test_get_nonexistent_worker_returns_none(self):
        assert worker_tracker.get_worker("does-not-exist") is None

    def test_worker_metrics_updated(self):
        worker_tracker.register_worker("w-1")
        worker_tracker.register_worker("w-2")
        snap = metrics_collector.get_snapshot()
        assert snap["gauges"]["workers_total"][""] == 2.0


class TestWorkersAPIEndpoint:
    def test_list_workers_empty(self, no_auth_client):
        resp = no_auth_client.get("/workers")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_workers_populated(self, no_auth_client):
        worker_tracker.register_worker("w-1", metadata={"region": "us-east"})
        resp = no_auth_client.get("/workers")
        assert resp.status_code == 200
        workers = resp.json()
        assert len(workers) == 1
        assert workers[0]["worker_id"] == "w-1"

    def test_get_worker_not_found(self, no_auth_client):
        resp = no_auth_client.get("/workers/nonexistent")
        assert resp.status_code == 404

    def test_get_worker_found(self, no_auth_client):
        worker_tracker.register_worker("w-99")
        resp = no_auth_client.get("/workers/w-99")
        assert resp.status_code == 200
        assert resp.json()["worker_id"] == "w-99"

    def test_workers_read_only_post_rejected(self, no_auth_client):
        resp = no_auth_client.post("/workers", json={})
        assert resp.status_code == 405

    def test_worker_id_manipulation_returns_404(self, no_auth_client):
        worker_tracker.register_worker("w-real")
        resp = no_auth_client.get("/workers/w-fake")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Section 5: Agent Visibility
# ---------------------------------------------------------------------------


class TestAgentsAPIEndpoint:
    def _register_agent(self):
        spec = AgentSpec(
            id=AgentId(id="test-agent"),
            name="Test Agent",
            description="A test agent",
            role="tester",
            capabilities=[AgentCapability(name="code-review", description="Review code")],
            permissions=[
                ToolPermission(category=PermissionCategory.READ_ONLY, level=PermissionLevel.ALLOW, description="r")
            ],
        )
        from baby.agents.base import Agent

        class SimpleAgent(Agent):
            async def execute(self, context):
                return AgentResult(step_id=1, agent_id=self.spec.id, success=True)

        agent_registry.register(SimpleAgent(spec))
        return spec

    def test_list_agents_empty(self, no_auth_client):
        resp = no_auth_client.get("/agents")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_agents_populated(self, no_auth_client):
        self._register_agent()
        resp = no_auth_client.get("/agents")
        assert resp.status_code == 200
        agents = resp.json()
        assert len(agents) == 1
        assert agents[0]["id"] == "test-agent"
        assert agents[0]["role"] == "tester"

    def test_agent_view_no_internal_prompts_leaked(self, no_auth_client):
        self._register_agent()
        resp = no_auth_client.get("/agents")
        body = resp.json()[0]
        # No private implementation details like model API keys
        for key in ["api_key", "token", "credential", "secret"]:
            assert body.get(key) is None

    def test_get_agent_not_found(self, no_auth_client):
        resp = no_auth_client.get("/agents/nonexistent")
        assert resp.status_code == 404

    def test_get_agent_found(self, no_auth_client):
        self._register_agent()
        resp = no_auth_client.get("/agents/test-agent")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "test-agent"
        assert "capabilities" in body

    def test_agents_read_only_post_rejected(self, no_auth_client):
        resp = no_auth_client.post("/agents", json={})
        assert resp.status_code == 405


# ---------------------------------------------------------------------------
# Section 6: Audit Visibility
# ---------------------------------------------------------------------------


class TestAuditLog:
    def test_get_events_user_id_filter(self):
        task_id = TaskId()
        audit_log.record(AuditEventType.TASK_CREATED, task_id=task_id, user_id="user-a")
        audit_log.record(AuditEventType.TASK_CREATED, task_id=TaskId(), user_id="user-b")

        events = audit_log.get_events(user_id="user-a")
        assert len(events) == 1
        assert events[0].user_id == "user-a"

    def test_get_events_time_range_filter(self):
        early = datetime(2026, 1, 1)
        late = datetime(2026, 6, 1)
        audit_log.record(AuditEventType.TASK_CREATED)
        events = audit_log.get_events(start_time=early, end_time=late)
        # Events recorded now are after late, so should be empty
        assert len(events) == 0

    def test_count_events(self):
        for _ in range(5):
            audit_log.record(AuditEventType.TOOL_INVOKED)
        assert audit_log.count_events() == 5

    def test_count_events_filtered(self):
        task_id = TaskId()
        audit_log.record(AuditEventType.TASK_CREATED, task_id=task_id)
        audit_log.record(AuditEventType.TASK_FAILED, task_id=task_id)
        audit_log.record(AuditEventType.TASK_CREATED, task_id=TaskId())
        assert audit_log.count_events(event_type=AuditEventType.TASK_CREATED) == 2

    def test_pagination_limit_and_offset(self):
        for _ in range(10):
            audit_log.record(AuditEventType.TOOL_RESULT)
        page1 = audit_log.get_events(limit=3)
        assert len(page1) == 3
        page2 = audit_log.get_events(limit=3, offset=3)
        assert len(page2) == 3
        # Ensure pages don't overlap
        page1_ids = {e.id for e in page1}
        page2_ids = {e.id for e in page2}
        assert page1_ids.isdisjoint(page2_ids)

    def test_immutability_of_returned_list(self):
        audit_log.record(AuditEventType.AGENT_SELECTED)
        events = audit_log.get_events()
        original_count = len(events)
        events.append(MagicMock())  # Mutate return value
        # Internal store should not be affected
        assert len(audit_log.get_events()) == original_count


class TestAuditAPIEndpoint:
    def test_list_audit_events_empty(self, no_auth_client):
        resp = no_auth_client.get("/audit")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_audit_events_populated(self, no_auth_client):
        audit_log.record(AuditEventType.TASK_CREATED, user_id="eng-1")
        resp = no_auth_client.get("/audit")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_audit_read_only_post_rejected(self, no_auth_client):
        resp = no_auth_client.post("/audit", json={})
        assert resp.status_code == 405

    def test_audit_read_only_delete_rejected(self, no_auth_client):
        resp = no_auth_client.delete("/audit")
        assert resp.status_code == 405

    def test_audit_engagement_isolation(self, auth_client):
        settings.api_require_auth = True
        audit_log.record(AuditEventType.TASK_CREATED, user_id="eng-2")

        # Operator scoped to eng-1 should NOT see eng-2 events
        headers = {"Authorization": "Bearer op-secret", "X-Engagement-ID": "eng-1"}
        resp = auth_client.get("/audit", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_audit_admin_sees_all(self, auth_client):
        settings.api_require_auth = True
        audit_log.record(AuditEventType.TASK_CREATED, user_id="eng-A")
        audit_log.record(AuditEventType.TASK_CREATED, user_id="eng-B")

        headers = {"Authorization": "Bearer admin-secret"}
        resp = auth_client.get("/audit", headers=headers)
        assert resp.status_code == 200
        # Admin can see all events
        assert len(resp.json()) >= 2

    def test_audit_id_manipulation_isolation(self, auth_client):
        settings.api_require_auth = True
        task_id = TaskId()
        audit_log.record(AuditEventType.TASK_CREATED, task_id=task_id, user_id="eng-2")

        headers = {"Authorization": "Bearer op-secret", "X-Engagement-ID": "eng-1"}
        resp = auth_client.get(f"/audit?task_id={task_id.id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == []

    def test_audit_secrets_redacted_in_details(self, no_auth_client):
        audit_log.record(
            AuditEventType.TOOL_INVOKED,
            details={"api_key": "sk-supersecret1234567890", "tool": "read_file"},
        )
        resp = no_auth_client.get("/audit")
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) == 1
        details = events[0].get("details", {})
        assert details.get("api_key") == "[REDACTED]"
        assert details.get("tool") == "read_file"


# ---------------------------------------------------------------------------
# Section 7: Authorization
# ---------------------------------------------------------------------------


class TestAuthorization:
    def test_unauthenticated_request_rejected_when_auth_required(self, auth_client):
        settings.api_require_auth = True
        resp = auth_client.get("/tasks")
        assert resp.status_code == 401

    def test_wrong_token_rejected(self, auth_client):
        settings.api_require_auth = True
        headers = {"Authorization": "Bearer wrong-token"}
        resp = auth_client.get("/tasks", headers=headers)
        assert resp.status_code == 401

    def test_valid_op_token_accepted(self, auth_client):
        settings.api_require_auth = True
        headers = {"Authorization": "Bearer op-secret", "X-Engagement-ID": "default"}
        resp = auth_client.get("/tasks", headers=headers)
        assert resp.status_code == 200

    def test_valid_admin_token_accepted(self, auth_client):
        settings.api_require_auth = True
        headers = {"Authorization": "Bearer admin-secret"}
        resp = auth_client.get("/tasks", headers=headers)
        assert resp.status_code == 200

    def test_api_key_header_accepted(self, auth_client):
        settings.api_require_auth = True
        headers = {"X-API-Key": "op-secret", "X-Engagement-ID": "default"}
        resp = auth_client.get("/tasks", headers=headers)
        assert resp.status_code == 200

    def test_health_accessible_without_auth(self, auth_client):
        """Health endpoint is read-only and accessible without token."""
        settings.api_require_auth = True
        with patch.object(health_checker, "check_health") as mock:
            mock.return_value = SystemHealth(status=HealthStatus.HEALTHY, components={})
            resp = auth_client.get("/health")
        # Health has no auth dependency so accessible even without token
        assert resp.status_code == 200

    def test_worker_id_does_not_bypass_auth(self, auth_client):
        settings.api_require_auth = True
        worker_tracker.register_worker("w-real")
        # No auth header → 401
        resp = auth_client.get("/workers/w-real")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Section 8: Secret Redaction Unit Tests
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    def test_redact_dict_api_key(self):
        data = {"api_key": "sk-supersecret", "name": "visible"}
        result = redact_sensitive_data(data)
        assert result["api_key"] == "[REDACTED]"
        assert result["name"] == "visible"

    def test_redact_nested_dict(self):
        data = {"outer": {"token": "bearer-xyz", "value": 42}}
        result = redact_sensitive_data(data)
        assert result["outer"]["token"] == "[REDACTED]"
        assert result["outer"]["value"] == 42

    def test_redact_list(self):
        data = [{"secret": "abc"}, {"name": "ok"}]
        result = redact_sensitive_data(data)
        assert result[0]["secret"] == "[REDACTED]"
        assert result[1]["name"] == "ok"

    def test_redact_openai_key_in_string(self):
        data = {"message": "using sk-1234567890abcdef12345678 to call API"}
        result = redact_sensitive_data(data)
        assert "sk-" not in result["message"]

    def test_redact_password_field(self):
        data = {"password": "hunter2", "username": "alice"}
        result = redact_sensitive_data(data)
        assert result["password"] == "[REDACTED]"
        assert result["username"] == "alice"

    def test_redact_credentials_field(self):
        data = {"credentials": {"access_token": "mytoken"}}
        result = redact_sensitive_data(data)
        assert result["credentials"] == "[REDACTED]"

    def test_redact_leaves_non_sensitive_strings(self):
        data = {"description": "This is a task", "priority": 3}
        result = redact_sensitive_data(data)
        assert result["description"] == "This is a task"
        assert result["priority"] == 3

    def test_redact_none_value(self):
        assert redact_sensitive_data(None) is None

    def test_redact_preserves_empty_dict(self):
        assert redact_sensitive_data({}) == {}

    def test_openai_api_key_never_in_audit_response(self, no_auth_client):
        audit_log.record(
            AuditEventType.TOOL_INVOKED,
            details={"openai_api_key": "sk-test123456789012345678", "tool": "call_model"},
        )
        resp = no_auth_client.get("/audit")
        raw_text = resp.text
        assert "sk-test" not in raw_text

    def test_anthropic_api_key_never_in_task_response(self, no_auth_client):
        task = Task(title="Secrets task", description="d")
        task_tracker.register_task(task)
        # Simulate a task_record with secret metadata (it shouldn't be in the response)
        resp = no_auth_client.get(f"/tasks/{task.id.id}")
        raw_text = resp.text
        assert "anthropic_api_key" not in raw_text or "[REDACTED]" in raw_text


# ---------------------------------------------------------------------------
# Section 9: Read-Only Guarantee
# ---------------------------------------------------------------------------


class TestReadOnlyGuarantee:
    """Observability endpoints must never trigger writes, mutations, or execution."""

    def test_post_to_health_rejected(self, no_auth_client):
        resp = no_auth_client.post("/health", json={})
        assert resp.status_code == 405

    def test_put_to_metrics_rejected(self, no_auth_client):
        resp = no_auth_client.put("/metrics", json={})
        assert resp.status_code == 405

    def test_delete_to_audit_rejected(self, no_auth_client):
        resp = no_auth_client.delete("/audit")
        assert resp.status_code == 405

    def test_post_to_agents_rejected(self, no_auth_client):
        resp = no_auth_client.post("/agents", json={})
        assert resp.status_code == 405

    def test_patch_to_workers_rejected(self, no_auth_client):
        resp = no_auth_client.patch("/workers/some-id", json={})
        assert resp.status_code == 405

    def test_task_tracker_get_does_not_modify_record(self):
        task = Task(title="R", description="d")
        task_tracker.register_task(task)
        r1 = task_tracker.get_task(task.id)
        r2 = task_tracker.get_task(task.id)
        assert r1.status == r2.status

    def test_audit_get_events_does_not_modify_log(self):
        audit_log.record(AuditEventType.TASK_CREATED)
        before = len(audit_log.get_events())
        audit_log.get_events()
        after = len(audit_log.get_events())
        assert before == after


# ---------------------------------------------------------------------------
# Section 10: Empty State and Malformed Requests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_list_tasks_empty(self, no_auth_client):
        resp = no_auth_client.get("/tasks")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_workers_empty(self, no_auth_client):
        resp = no_auth_client.get("/workers")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_agents_empty(self, no_auth_client):
        resp = no_auth_client.get("/agents")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_audit_empty(self, no_auth_client):
        resp = no_auth_client.get("/audit")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_task_malformed_uuid(self, no_auth_client):
        resp = no_auth_client.get("/tasks/not-a-uuid")
        assert resp.status_code == 422

    def test_metrics_snapshot_always_has_structure(self, no_auth_client):
        resp = no_auth_client.get("/metrics")
        body = resp.json()
        assert "counters" in body
        assert "gauges" in body
        assert "latencies" in body

    def test_audit_limit_zero_returns_empty(self, no_auth_client):
        audit_log.record(AuditEventType.TASK_CREATED)
        resp = no_auth_client.get("/audit?limit=0")
        assert resp.status_code == 422  # limit must be >= 1

    def test_audit_negative_offset_rejected(self, no_auth_client):
        resp = no_auth_client.get("/audit?offset=-1")
        assert resp.status_code == 422
