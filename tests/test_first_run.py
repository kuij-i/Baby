"""First-run experience regressions found by running the documented walkthrough end to end.

Covers:
- Logging: ``configure_logging`` renders each event once (no double-encoded JSON), honors
  ``LOG_LEVEL`` and writes to the current ``sys.stderr``
- Health/readiness in the split deployment: agents live in durable worker processes, so a
  live worker makes the agents component healthy even though the API registers none
- Redaction keeps token *counters* (``tokens_used``) intact so the typed contract holds
- ``GET /`` landing response and the README task-submission payload
"""

import io
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
import structlog
from fastapi.testclient import TestClient

from baby.agents import agent_registry
from baby.audit import audit_log
from baby.configuration import settings
from baby.core import utc_now
from baby.logging import configure_logging, get_logger, resolve_log_level
from baby.observability.health import HealthStatus, count_live_durable_workers, health_checker
from baby.observability.redaction import REDACTED_VALUE, redact_sensitive_data
from baby.observability.tasks import task_tracker
from baby.permissions import permission_manager
from baby.persistence.database import db_manager
from baby.persistence.sqlite_workers import SQLiteWorkerStore

ADMIN_SECRET = "admin-secret-for-tests-0123456789abcdef"
OP_SECRET = "op-secret-for-tests-0123456789abcdef"


@pytest.fixture(autouse=True)
def restore_structlog():
    """Logging configuration is process-global; put the default back after each test."""
    yield
    structlog.reset_defaults()


@pytest.fixture
def durable_db(tmp_path: Path):
    original_url = db_manager.database_url
    db_manager.configure(f"sqlite:///{tmp_path / 'baby.db'}")
    db_manager.init_schema()
    for reset in (task_tracker.clear, audit_log.clear, agent_registry.clear, permission_manager.clear):
        reset()
    yield
    audit_log.use_backend(None)
    audit_log.clear()
    agent_registry.clear()
    db_manager.configure(original_url)
    db_manager.init_schema()


@pytest.fixture
def no_auth_client():
    from baby.api.app import app

    with patch.multiple(
        settings,
        api_require_auth=False,
        development_mode=True,
        api_admin_token=ADMIN_SECRET,
        api_auth_token=OP_SECRET,
    ):
        yield TestClient(app, raise_server_exceptions=False)


def _register_worker(heartbeat_age_seconds: float = 0.0, status: str = "idle") -> str:
    store = task_tracker.store
    assert store is not None
    workers = SQLiteWorkerStore(store.db)
    worker_id = f"worker-{uuid4().hex}"
    seen = utc_now() - timedelta(seconds=heartbeat_age_seconds)
    workers.register(worker_id, "host", 1234, seen)
    with store.db.transaction() as conn:
        conn.execute("UPDATE workers SET status = ? WHERE worker_id = ?", (status, worker_id))
    return worker_id


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class TestLogging:
    def test_event_rendered_once_with_level_and_context(self):
        buffer = io.StringIO()
        configure_logging("INFO")
        with patch.object(sys, "stderr", buffer):
            get_logger("baby.test").info("Worker supervisor stopped", workers=2)
        line = buffer.getvalue().strip()
        assert "Worker supervisor stopped" in line
        assert "workers=2" in line
        assert "info" in line
        # The previous configuration JSON-encoded structlog's internal (args, kwargs) tuple.
        assert "_logger" not in line and "_FixedFindCallerLogger" not in line
        assert line.count("Worker supervisor stopped") == 1

    def test_level_filtering(self):
        buffer = io.StringIO()
        configure_logging("WARNING")
        logger = get_logger("baby.test")
        with patch.object(sys, "stderr", buffer):
            logger.info("hidden-info")
            logger.debug("hidden-debug")
            logger.warning("shown-warning")
            logger.error("shown-error", exc=ValueError("boom"))
        output = buffer.getvalue()
        assert "hidden-info" not in output and "hidden-debug" not in output
        assert "shown-warning" in output
        assert "shown-error" in output and "boom" in output

    def test_writes_to_current_stderr_after_configuration(self):
        configure_logging("INFO")
        later = io.StringIO()
        with patch.object(sys, "stderr", later):
            get_logger("baby.test").info("after-swap")
        assert "after-swap" in later.getvalue()

    @pytest.mark.parametrize(
        "name, expected", [("debug", 10), ("INFO", 20), (" warning ", 30), ("bogus", 20), ("", 20)]
    )
    def test_resolve_log_level(self, name, expected):
        assert resolve_log_level(name) == expected

    def test_worker_process_applies_forwarded_log_level(self):
        from baby.workers.process import FORWARDABLE_SETTINGS, WorkerProcessConfig, _run

        assert "log_level" in FORWARDABLE_SETTINGS
        config = WorkerProcessConfig(
            worker_id="worker-x",
            database_url="sqlite:///unused.db",
            # in_memory makes _run refuse to start right after logging is configured,
            # before it touches the (process-global) database manager.
            settings_overrides={"log_level": "ERROR", "persistence_backend": "in_memory"},
        )
        with (
            patch("baby.logging.configure_logging") as configure,
            patch.object(settings, "log_level", settings.log_level),
            patch.object(settings, "persistence_backend", settings.persistence_backend),
            patch.object(db_manager, "configure") as db_configure,
            patch.object(db_manager, "close"),
        ):
            assert _run(config, None) == 2
        configure.assert_called_once_with("ERROR")
        db_configure.assert_not_called()


# ---------------------------------------------------------------------------
# Health / readiness in the split deployment
# ---------------------------------------------------------------------------


class TestHealthWithDurableWorkers:
    def test_no_agents_and_no_workers_is_degraded(self, durable_db):
        health = health_checker.check_agents()
        assert health.status == HealthStatus.DEGRADED
        assert health.details["live_worker_processes"] == 0

    def test_live_worker_makes_agents_healthy(self, durable_db):
        _register_worker()
        health = health_checker.check_agents()
        assert health.status == HealthStatus.HEALTHY
        assert health.details["live_worker_processes"] == 1
        assert "worker process" in health.message

    def test_stale_or_stopped_workers_do_not_count(self, durable_db):
        _register_worker(heartbeat_age_seconds=settings.worker_stale_timeout_seconds + 60)
        _register_worker(status="stopped")
        _register_worker(status="error")
        assert count_live_durable_workers() == 0
        assert health_checker.check_agents().status == HealthStatus.DEGRADED

    def test_registry_read_failure_counts_zero(self, durable_db):
        with patch.object(SQLiteWorkerStore, "list", side_effect=RuntimeError("db gone")):
            assert count_live_durable_workers() == 0

    def test_in_memory_backend_counts_zero(self):
        with patch.object(type(task_tracker), "store", new=property(lambda self: None)):
            assert count_live_durable_workers() == 0

    def test_readiness_reports_live_worker_processes(self, durable_db, no_auth_client):
        _register_worker()
        response = no_auth_client.get("/api/v1/readiness")
        assert response.status_code == 200
        assert response.json()["details"]["workers"]["live_worker_processes"] == 1


# ---------------------------------------------------------------------------
# Redaction must not break the typed contract
# ---------------------------------------------------------------------------


class TestTokenCounters:
    def test_token_counters_are_not_redacted(self):
        data = {"tokens_used": 42, "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
        assert redact_sensitive_data(data) == data

    def test_real_tokens_still_redacted(self):
        data = {"token": "abc", "access_token": "abc", "api_token": "abc", "tokens_used": "sk-" + "a" * 30}
        result = redact_sensitive_data(data)
        assert result["token"] == REDACTED_VALUE
        assert result["access_token"] == REDACTED_VALUE
        assert result["api_token"] == REDACTED_VALUE
        # A secret-looking string is still scrubbed even under an allowed key.
        assert "sk-aaaa" not in result["tokens_used"]

    def test_task_view_returns_integer_tokens_used(self, durable_db, no_auth_client):
        from baby.core import AgentId, AgentResult, Task, TaskStatus

        task = Task(title="t", description="d", user_id="default")
        task_tracker.register_task(task)
        task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)
        task_tracker.update_status(
            task.id,
            TaskStatus.COMPLETED,
            result=AgentResult(step_id=1, agent_id=AgentId(id="coding-agent"), success=True, tokens_used=105),
        )
        body = no_auth_client.get(f"/api/v1/tasks/{task.id.id}").json()
        assert body["result"]["tokens_used"] == 105


# ---------------------------------------------------------------------------
# Landing page and documented submission payload
# ---------------------------------------------------------------------------


class TestFirstRunEndpoints:
    def test_root_is_static_landing_response(self, no_auth_client):
        response = no_auth_client.get("/")
        assert response.status_code == 200
        assert response.json() == {
            "name": "BABY Observability API",
            "version": "0.1.0",
            "api": "/api/v1",
            "health": "/api/v1/health",
            "docs": "/docs",
        }

    def test_root_not_in_openapi_contract(self, no_auth_client):
        assert "/" not in no_auth_client.get("/openapi.json").json()["paths"]

    def test_readme_operator_submission_payload_is_accepted(self, durable_db):
        from baby.api.app import app

        with patch.multiple(
            settings,
            api_require_auth=True,
            development_mode=False,
            api_admin_token=ADMIN_SECRET,
            api_auth_token=OP_SECRET,
            operator_engagements="default",
        ):
            response = TestClient(app).post(
                "/api/v1/tasks/submit",
                json={"title": "First run", "description": "list files", "priority": 5},
                headers={"Authorization": f"Bearer {OP_SECRET}"},
            )
        assert response.status_code == 202
        assert response.json()["status"] == "pending"
        assert response.json()["engagement_id"] == "default"

    def test_readme_development_mode_payload_names_engagement(self, durable_db, no_auth_client):
        payload = {"title": "First run", "description": "list files", "priority": 5}
        assert no_auth_client.post("/api/v1/tasks/submit", json=payload).status_code == 422
        response = no_auth_client.post("/api/v1/tasks/submit", json={**payload, "engagement_id": "default"})
        assert response.status_code == 202

    def test_submission_without_title_is_rejected(self, durable_db, no_auth_client):
        response = no_auth_client.post("/api/v1/tasks/submit", json={"description": "list files", "priority": 5})
        assert response.status_code == 422


class TestProviderVisibility:
    def test_api_startup_registers_configured_provider_for_health(self, durable_db, no_auth_client):
        from baby.providers import provider_registry

        with patch.object(settings, "openai_api_key", "sk-test-" + "x" * 32):
            try:
                with no_auth_client as client:
                    providers = client.get("/api/v1/health").json()["components"]["providers"]
                assert providers["status"] == "healthy"
            finally:
                provider_registry.clear()

    def test_without_key_health_explains_how_to_enable(self, durable_db, no_auth_client):
        with patch.object(settings, "openai_api_key", None), no_auth_client as client:
            providers = client.get("/api/v1/health").json()["components"]["providers"]
        assert providers["status"] == "degraded"
        assert ".env" in providers["message"]
        assert "api_key" not in providers["message"].lower()
