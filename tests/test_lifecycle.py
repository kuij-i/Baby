"""Phase 10 Production Reliability & Lifecycle Hardening Tests.

Verifies:
- Task state machine transitions and terminal state enforcement
- Fail-closed worker crash/restart recovery (recover_stale_tasks)
- Readiness endpoint (/ready and /readiness) distinct from liveness (/health)
- Configuration validation (validate_settings) and security/bounds guarantees
- Lifespan startup and shutdown behavior
- Read-only guarantees and error boundaries on new endpoints
"""

from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from baby.api.app import app, lifespan
from baby.configuration import Settings, validate_settings
from baby.core import VALID_TASK_TRANSITIONS, Task, TaskStatus, WorkerStatus
from baby.errors import ConfigurationError, InvalidStateTransitionError
from baby.observability.health import ComponentHealth, HealthStatus, health_checker
from baby.observability.tasks import task_tracker
from baby.observability.workers import worker_tracker


@pytest.fixture(autouse=True)
def reset_trackers():
    """Reset task and worker trackers before and after each test."""
    task_tracker.clear()
    worker_tracker.clear()
    yield
    task_tracker.clear()
    worker_tracker.clear()


@pytest.fixture
def client():
    """TestClient for API requests with auth disabled for test convenience."""
    with patch("baby.configuration.settings.api_require_auth", False):
        with TestClient(app) as c:
            yield c


# ============================================================================
# 1. TASK STATE MACHINE HARDENING
# ============================================================================


class TestTaskStateMachine:
    """Validate task state machine rules and transition enforcement."""

    def test_valid_lifecycle_pending_to_in_progress_to_completed(self):
        task = Task(title="Lifecycle Test", description="test desc")
        task_tracker.register_task(task, status=TaskStatus.PENDING)

        in_prog = task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)
        assert in_prog is not None
        assert in_prog.status == TaskStatus.IN_PROGRESS

        completed = task_tracker.update_status(task.id, TaskStatus.COMPLETED)
        assert completed is not None
        assert completed.status == TaskStatus.COMPLETED
        assert completed.completed_at is not None

    def test_valid_lifecycle_in_progress_to_failed(self):
        task = Task(title="Fail Test", description="test desc")
        task_tracker.register_task(task, status=TaskStatus.PENDING)
        task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)

        failed = task_tracker.update_status(task.id, TaskStatus.FAILED, error="execution error")
        assert failed is not None
        assert failed.status == TaskStatus.FAILED
        assert failed.error == "execution error"
        assert failed.completed_at is not None

    def test_valid_lifecycle_awaiting_approval(self):
        task = Task(title="Approval Test", description="test desc")
        task_tracker.register_task(task, status=TaskStatus.PENDING)
        task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)

        awaiting = task_tracker.update_status(task.id, TaskStatus.AWAITING_APPROVAL)
        assert awaiting is not None
        assert awaiting.status == TaskStatus.AWAITING_APPROVAL

        # Resume after approval
        resumed = task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)
        assert resumed is not None
        assert resumed.status == TaskStatus.IN_PROGRESS

        completed = task_tracker.update_status(task.id, TaskStatus.COMPLETED)
        assert completed is not None
        assert completed.status == TaskStatus.COMPLETED

    def test_valid_cancellation_from_pending(self):
        task = Task(title="Cancel Test", description="test desc")
        task_tracker.register_task(task, status=TaskStatus.PENDING)

        cancelled = task_tracker.update_status(task.id, TaskStatus.CANCELLED)
        assert cancelled is not None
        assert cancelled.status == TaskStatus.CANCELLED
        assert cancelled.completed_at is not None

    def test_valid_cancellation_from_in_progress(self):
        task = Task(title="Cancel IP Test", description="test desc")
        task_tracker.register_task(task, status=TaskStatus.PENDING)
        task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)

        cancelled = task_tracker.update_status(task.id, TaskStatus.CANCELLED)
        assert cancelled is not None
        assert cancelled.status == TaskStatus.CANCELLED

    def test_invalid_transition_pending_directly_to_completed_raises(self):
        task = Task(title="Invalid Transition", description="test desc")
        task_tracker.register_task(task, status=TaskStatus.PENDING)

        with pytest.raises(InvalidStateTransitionError) as exc_info:
            task_tracker.update_status(task.id, TaskStatus.COMPLETED)
        assert "Invalid task state transition" in str(exc_info.value)

    def test_invalid_transition_pending_directly_to_failed_raises(self):
        task = Task(title="Invalid Transition", description="test desc")
        task_tracker.register_task(task, status=TaskStatus.PENDING)

        with pytest.raises(InvalidStateTransitionError):
            task_tracker.update_status(task.id, TaskStatus.FAILED)

    def test_terminal_state_completed_cannot_transition(self):
        task = Task(title="Terminal Completed", description="test desc")
        task_tracker.register_task(task, status=TaskStatus.PENDING)
        task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)
        task_tracker.update_status(task.id, TaskStatus.COMPLETED)

        for target in [TaskStatus.IN_PROGRESS, TaskStatus.FAILED, TaskStatus.PENDING, TaskStatus.CANCELLED]:
            with pytest.raises(InvalidStateTransitionError) as exc_info:
                task_tracker.update_status(task.id, target)
            assert "terminal state" in str(exc_info.value)

    def test_terminal_state_failed_cannot_transition(self):
        task = Task(title="Terminal Failed", description="test desc")
        task_tracker.register_task(task, status=TaskStatus.PENDING)
        task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)
        task_tracker.update_status(task.id, TaskStatus.FAILED)

        for target in [TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED, TaskStatus.PENDING]:
            with pytest.raises(InvalidStateTransitionError) as exc_info:
                task_tracker.update_status(task.id, target)
            assert "terminal state" in str(exc_info.value)

    def test_terminal_state_cancelled_cannot_transition(self):
        task = Task(title="Terminal Cancelled", description="test desc")
        task_tracker.register_task(task, status=TaskStatus.PENDING)
        task_tracker.update_status(task.id, TaskStatus.CANCELLED)

        for target in [TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED, TaskStatus.FAILED]:
            with pytest.raises(InvalidStateTransitionError) as exc_info:
                task_tracker.update_status(task.id, target)
            assert "terminal state" in str(exc_info.value)

    def test_valid_task_transitions_table_integrity(self):
        """Ensure all states in TaskStatus are mapped in VALID_TASK_TRANSITIONS."""
        for status in TaskStatus:
            assert status in VALID_TASK_TRANSITIONS
        # Terminal states must have empty allowed transitions
        assert VALID_TASK_TRANSITIONS[TaskStatus.COMPLETED] == set()
        assert VALID_TASK_TRANSITIONS[TaskStatus.FAILED] == set()
        assert VALID_TASK_TRANSITIONS[TaskStatus.CANCELLED] == set()


# ============================================================================
# 2. WORKER RESTART & CRASH RECOVERY (FAIL-CLOSED)
# ============================================================================


class TestCrashRecovery:
    """Validate deterministic fail-closed recovery for interrupted tasks."""

    def test_recover_stale_tasks_marks_in_progress_as_failed(self):
        # Setup: task 1 pending, task 2 in progress, task 3 completed
        t1 = Task(title="T1", description="pending")
        t2 = Task(title="T2", description="interrupted in progress")
        t3 = Task(title="T3", description="completed")

        task_tracker.register_task(t1, status=TaskStatus.PENDING)

        task_tracker.register_task(t2, status=TaskStatus.PENDING)
        task_tracker.update_status(t2.id, TaskStatus.IN_PROGRESS)

        task_tracker.register_task(t3, status=TaskStatus.PENDING)
        task_tracker.update_status(t3.id, TaskStatus.IN_PROGRESS)
        task_tracker.update_status(t3.id, TaskStatus.COMPLETED)

        # Run recovery
        recovered = task_tracker.recover_stale_tasks()

        assert len(recovered) == 1
        assert recovered[0].task_id == t2.id
        assert recovered[0].status == TaskStatus.FAILED
        assert "restart" in recovered[0].error.lower()
        assert recovered[0].completed_at is not None

        # Verify other tasks were untouched
        assert task_tracker.get_task(t1.id).status == TaskStatus.PENDING
        assert task_tracker.get_task(t3.id).status == TaskStatus.COMPLETED

    def test_recover_stale_tasks_when_none_in_progress(self):
        t1 = Task(title="T1", description="pending")
        task_tracker.register_task(t1, status=TaskStatus.PENDING)

        recovered = task_tracker.recover_stale_tasks()
        assert recovered == []
        assert task_tracker.get_task(t1.id).status == TaskStatus.PENDING


# ============================================================================
# 3. READINESS VS LIVENESS ENDPOINTS
# ============================================================================


class TestReadinessAndLiveness:
    """Verify distinct readiness and liveness semantics."""

    def test_health_liveness_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert "status" in body
        assert "components" in body

    def test_readiness_ready_returns_200(self, client):
        resp = client.get("/readiness")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is True
        assert body["status"] == "ready"
        assert "details" in body
        assert body["issues"] == []

    def test_ready_alias_returns_200(self, client):
        resp = client.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is True
        assert body["status"] == "ready"

    def test_readiness_fails_when_memory_unavailable(self, client):
        unavailable_component = ComponentHealth(
            status=HealthStatus.UNAVAILABLE,
            message="Memory store unavailable",
        )
        with patch.object(health_checker, "check_memory", return_value=unavailable_component):
            resp = client.get("/readiness")
            assert resp.status_code == 503
            body = resp.json()
            assert body["ready"] is False
            assert body["status"] == "not_ready"
            assert any("Persistence" in issue or "memory" in issue.lower() for issue in body["issues"])

    def test_readiness_fails_when_auth_required_without_tokens(self, client):
        with patch("baby.configuration.settings.api_require_auth", True):
            with patch("baby.configuration.settings.api_auth_token", None):
                with patch("baby.configuration.settings.api_admin_token", None):
                    resp = client.get("/ready")
                    assert resp.status_code == 503
                    body = resp.json()
                    assert body["ready"] is False
                    assert any("tokens" in issue for issue in body["issues"])

    def test_readiness_endpoints_are_strictly_read_only(self, client):
        for path in ["/ready", "/readiness"]:
            assert client.post(path, json={}).status_code == 405
            assert client.put(path, json={}).status_code == 405
            assert client.delete(path).status_code == 405


# ============================================================================
# 4. CONFIGURATION VALIDATION
# ============================================================================


class TestConfigurationValidation:
    """Validate early configuration boundary checking."""

    def test_valid_default_settings(self):
        cfg = Settings()
        warnings = validate_settings(cfg)
        assert isinstance(warnings, list)

    def test_invalid_coding_max_iterations_raises(self):
        cfg = Settings()
        cfg.coding_max_iterations = 0
        with pytest.raises(ConfigurationError) as exc_info:
            validate_settings(cfg)
        assert "coding_max_iterations" in str(exc_info.value)

    def test_invalid_coding_max_tool_calls_raises(self):
        cfg = Settings()
        cfg.coding_max_tool_calls_per_iteration = 0
        with pytest.raises(ConfigurationError) as exc_info:
            validate_settings(cfg)
        assert "coding_max_tool_calls_per_iteration" in str(exc_info.value)

    def test_invalid_file_size_bytes_raises(self):
        cfg = Settings()
        cfg.coding_max_file_size_bytes = 0
        with pytest.raises(ConfigurationError) as exc_info:
            validate_settings(cfg)
        assert "coding_max_file_size_bytes" in str(exc_info.value)

    def test_dev_mode_produces_warning(self):
        cfg = Settings()
        cfg.api_require_auth = False
        warnings = validate_settings(cfg)
        assert any("development mode" in w for w in warnings)

    def test_no_secrets_in_warnings(self):
        cfg = Settings()
        cfg.openai_api_key = "super-secret-openai-key-12345"
        cfg.api_auth_token = "super-secret-auth-token-67890"
        warnings = validate_settings(cfg)
        for w in warnings:
            assert "super-secret" not in w


# ============================================================================
# 5. LIFESPAN STARTUP AND SHUTDOWN
# ============================================================================


class TestLifespan:
    """Validate graceful startup and shutdown lifecycle management."""

    @pytest.mark.asyncio
    async def test_lifespan_recovers_stale_tasks_at_startup(self):
        # Create an in-progress task before startup
        task = Task(title="Pre-crash task", description="desc")
        task_tracker.register_task(task, status=TaskStatus.PENDING)
        task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)

        # Run lifespan context
        async with lifespan(app):
            # During running state, task should have been recovered as FAILED
            rec = task_tracker.get_task(task.id)
            assert rec is not None
            assert rec.status == TaskStatus.FAILED
            assert "restart" in rec.error.lower()

    @pytest.mark.asyncio
    async def test_lifespan_stops_workers_on_shutdown(self):
        # Register an active worker
        worker = worker_tracker.register_worker("worker-1", status=WorkerStatus.IDLE)
        assert worker.status == WorkerStatus.IDLE

        # Run lifespan and complete it (triggers shutdown)
        async with lifespan(app):
            pass  # app is running

        # After shutdown, active worker must be transitioned to STOPPED
        w = worker_tracker.get_worker("worker-1")
        assert w is not None
        assert w.status == WorkerStatus.STOPPED


# ============================================================================
# 6. API ERROR BOUNDARIES
# ============================================================================


class TestApiErrorBoundaries:
    """Validate that API gracefully handles unexpected errors without leaking traces."""

    def test_malformed_task_id_returns_404(self, client):
        resp = client.get("/tasks/not-a-valid-uuid")
        # FastAPI path parameter validation returns 422 or 404
        assert resp.status_code in (404, 422)

    def test_nonexistent_task_id_returns_404(self, client):
        resp = client.get(f"/tasks/{uuid4()}")
        assert resp.status_code == 404

    def test_internal_server_error_does_not_leak_stack_trace(self, client):
        with patch.object(task_tracker, "list_tasks", side_effect=RuntimeError("internal db crash")):
            resp = client.get("/tasks")
            assert resp.status_code == 500
            body = resp.json()
            assert body["detail"] == "Internal server error"
            assert "internal db crash" not in str(body)
            assert "Traceback" not in str(body)
