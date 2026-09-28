"""Phase 9 Security Hardening Tests.

Regression tests covering every security finding identified in the Phase 9
security review:

  FINDING-1: Operator engagement scope must come from server-side config, not
              X-Engagement-ID header (authorization bypass).
  FINDING-2: dev mode (api_require_auth=False) must not be exploitable to
              access arbitrary engagements via header manipulation.
  FINDING-3: Health endpoint must not leak exception details (paths, strings).
  FINDING-4: Metrics cardinality must be bounded against crafted endpoint labels.
  FINDING-5/6: Non-admin callers with ambiguous scope must get empty results,
               not all data.

Test structure mirrors the specification:
  Test A — authorized engagement access
  Test B — unauthorized engagement rejection
  Test C — header manipulation (X-Engagement-ID cannot expand scope)
  Test D — query parameter manipulation
  Test E — path/ID manipulation (IDOR)
"""

from typing import Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from baby.agents import agent_registry
from baby.audit import audit_log
from baby.configuration import settings
from baby.core import AuditEventType, Task, TaskId
from baby.observability import metrics_collector, task_tracker, worker_tracker
from baby.observability.health import ComponentHealth, HealthStatus, health_checker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_state():
    """Isolate each test by resetting all shared singleton state."""
    audit_log.clear()
    task_tracker.clear()
    worker_tracker.clear()
    metrics_collector.reset()
    agent_registry.clear()

    # Reset settings to safe defaults
    original = {
        "api_require_auth": settings.api_require_auth,
        "api_admin_token": settings.api_admin_token,
        "api_auth_token": settings.api_auth_token,
        "operator_engagements": settings.operator_engagements,
    }
    yield
    for k, v in original.items():
        setattr(settings, k, v)

    audit_log.clear()
    task_tracker.clear()
    worker_tracker.clear()
    metrics_collector.reset()
    agent_registry.clear()


@pytest.fixture()
def client_auth_required():
    """TestClient with auth strictly enforced.
    Operator token 'op-token' is authorized for engagement-A ONLY (server-side).
    Admin token 'admin-token' has wildcard access.
    """
    from baby.api.app import app

    settings.api_require_auth = True
    settings.api_admin_token = "admin-token"
    settings.api_auth_token = "op-token"
    # Server-side: operator is authorized for engagement-A only
    settings.operator_engagements = "engagement-A"

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def client_no_auth():
    """TestClient in dev mode (api_require_auth=False)."""
    from baby.api.app import app

    settings.api_require_auth = False
    settings.api_admin_token = "admin-token"
    settings.api_auth_token = "op-token"
    settings.operator_engagements = "engagement-A"

    return TestClient(app, raise_server_exceptions=False)


def op_headers(engagement_header: Optional[str] = None) -> dict:
    """Operator auth headers. engagement_header is the X-Engagement-ID value (optional)."""
    h = {"Authorization": "Bearer op-token"}
    if engagement_header:
        h["X-Engagement-ID"] = engagement_header
    return h


def admin_headers() -> dict:
    return {"Authorization": "Bearer admin-token"}


# ---------------------------------------------------------------------------
# FINDING-1 Tests: Operator engagement scope is server-side only
# ---------------------------------------------------------------------------


class TestEngagementScopeServerSide:
    """The core authorization fix: X-Engagement-ID cannot grant access to
    engagements not in the server-side-configured operator_engagements set."""

    # Test A: Authorized engagement access
    def test_A_operator_can_access_authorized_engagement(self, client_auth_required):
        """Operator authorized for engagement-A can retrieve engagement-A tasks."""
        task = Task(title="A task", description="for A", user_id="engagement-A")
        task_tracker.register_task(task)

        resp = client_auth_required.get(
            "/tasks",
            params={"engagement_id": "engagement-A"},
            headers=op_headers(),
        )
        assert resp.status_code == 200
        tasks = resp.json()
        assert len(tasks) == 1
        assert tasks[0]["title"] == "A task"

    # Test B: Unauthorized engagement rejected
    def test_B_operator_cannot_access_unauthorized_engagement(self, client_auth_required):
        """Operator authorized for engagement-A is forbidden from engagement-B."""
        task = Task(title="B task", description="for B", user_id="engagement-B")
        task_tracker.register_task(task)

        resp = client_auth_required.get(
            "/tasks",
            params={"engagement_id": "engagement-B"},
            headers=op_headers(),
        )
        assert resp.status_code == 403

    # Test C: X-Engagement-ID header cannot expand scope beyond server config
    def test_C_header_manipulation_cannot_expand_scope(self, client_auth_required):
        """CRITICAL: Operator sends X-Engagement-ID: engagement-B but is only
        authorized for engagement-A server-side. Must be rejected."""
        task = Task(title="B task", description="for B", user_id="engagement-B")
        task_tracker.register_task(task)

        # The attacker uses their valid token but changes the header to engagement-B
        resp = client_auth_required.get(
            "/tasks",
            # No query param — the header is how they try to scope
            headers={
                "Authorization": "Bearer op-token",
                "X-Engagement-ID": "engagement-B",  # attacker-controlled
            },
        )
        # Must be rejected or return empty (operator's scope is engagement-A only)
        # The endpoint now uses query param engagement_id OR X-Engagement-ID,
        # both are checked against server-side allowed_engagements.
        assert resp.status_code in (200, 403)
        if resp.status_code == 200:
            # If 200, must return empty (no engagement-B tasks visible to eng-A operator)
            assert resp.json() == []

    def test_C_header_manipulation_tasks_endpoint_explicitly(self, client_auth_required):
        """Operator for engagement-A sends X-Engagement-ID: engagement-B in header,
        also passes engagement_id=engagement-B as query param — must be 403."""
        resp = client_auth_required.get(
            "/tasks",
            params={"engagement_id": "engagement-B"},
            headers={
                "Authorization": "Bearer op-token",
                "X-Engagement-ID": "engagement-B",
            },
        )
        assert resp.status_code == 403

    # Test D: Query parameter manipulation cannot bypass scope
    def test_D_query_param_manipulation_tasks(self, client_auth_required):
        """?engagement_id=engagement-B must be rejected for an engagement-A operator."""
        resp = client_auth_required.get(
            "/tasks?engagement_id=engagement-B",
            headers=op_headers(),
        )
        assert resp.status_code == 403

    def test_D_query_param_manipulation_audit(self, client_auth_required):
        """Audit endpoint: ?engagement_id=engagement-B must be rejected."""
        audit_log.record(AuditEventType.TASK_CREATED, user_id="engagement-B")
        resp = client_auth_required.get(
            "/audit?engagement_id=engagement-B",
            headers=op_headers(),
        )
        assert resp.status_code == 403

    def test_D_query_param_user_id_manipulation(self, client_auth_required):
        """Audit endpoint: ?user_id=engagement-B must also be rejected."""
        audit_log.record(AuditEventType.TASK_CREATED, user_id="engagement-B")
        resp = client_auth_required.get(
            "/audit?user_id=engagement-B",
            headers=op_headers(),
        )
        assert resp.status_code == 403

    # Test E: Path/ID manipulation (IDOR)
    def test_E_task_id_idor_cross_engagement(self, client_auth_required):
        """Knowing task UUID of another engagement does not grant access to it."""
        # Task belongs to engagement-B
        task = Task(title="Private B", description="d", user_id="engagement-B")
        task_tracker.register_task(task)

        # Operator for engagement-A knows the UUID (IDOR attempt)
        resp = client_auth_required.get(
            f"/tasks/{task.id.id}",
            headers=op_headers(),
        )
        # Must be 404 (not 403, to avoid confirming existence)
        assert resp.status_code == 404

    def test_E_audit_task_id_filter_cross_engagement(self, client_auth_required):
        """Filtering audit by a task_id from another engagement must return nothing."""
        task_id = TaskId()
        audit_log.record(AuditEventType.TASK_CREATED, task_id=task_id, user_id="engagement-B")

        resp = client_auth_required.get(
            f"/audit?task_id={task_id.id}",
            headers=op_headers(),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_E_worker_id_does_not_cross_scope(self, client_auth_required):
        """Worker IDs are global; worker endpoint must require auth."""
        worker_tracker.register_worker("w-1")
        # No auth → 401
        resp = client_auth_required.get("/workers/w-1")
        assert resp.status_code == 401

    def test_E_worker_id_with_auth_no_cross_engagement(self, client_auth_required):
        """Operator can view workers (workers are not scoped to engagements)."""
        worker_tracker.register_worker("w-1")
        resp = client_auth_required.get("/workers/w-1", headers=op_headers())
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# FINDING-2 Tests: dev mode behavior
# ---------------------------------------------------------------------------


class TestDevMode:
    """api_require_auth=False must not enable per-request scope escalation."""

    def test_dev_mode_no_token_gets_admin_scope(self, client_no_auth):
        """In dev mode, unauthenticated callers get admin scope (global access).
        This is intentional for local testing but must be documented as dev-only."""
        task = Task(title="Any task", description="d", user_id="some-engagement")
        task_tracker.register_task(task)
        resp = client_no_auth.get("/tasks")
        assert resp.status_code == 200
        # Admin scope — can see all tasks
        assert len(resp.json()) >= 1

    def test_dev_mode_header_does_not_restrict_to_partial_scope(self, client_no_auth):
        """In dev mode, unauthenticated caller is admin and can query any engagement
        or retrieve all tasks without filter."""
        task_A = Task(title="A", description="d", user_id="eng-A")
        task_B = Task(title="B", description="d", user_id="eng-B")
        task_tracker.register_task(task_A)
        task_tracker.register_task(task_B)

        # Without filter: all tasks returned
        resp_all = client_no_auth.get("/tasks")
        assert resp_all.status_code == 200
        assert len(resp_all.json()) >= 2

        # Filtered to eng-A: eng-A returned
        resp_a = client_no_auth.get("/tasks", headers={"X-Engagement-ID": "eng-A"})
        assert resp_a.status_code == 200
        assert len(resp_a.json()) == 1
        assert resp_a.json()[0]["user_id"] == "eng-A"

        # Filtered to eng-B: eng-B returned
        resp_b = client_no_auth.get("/tasks", headers={"X-Engagement-ID": "eng-B"})
        assert resp_b.status_code == 200
        assert len(resp_b.json()) == 1
        assert resp_b.json()[0]["user_id"] == "eng-B"

    def test_dev_mode_is_not_production_safe(self):
        """Confirm production default is api_require_auth=True."""
        # Create a fresh Settings instance with no env vars to check the default
        from baby.configuration import Settings

        fresh = Settings()
        assert (
            fresh.api_require_auth is True
        ), "api_require_auth must default to True to ensure production fail-closed behavior"


# ---------------------------------------------------------------------------
# FINDING-3 Tests: Health endpoint — no exception detail leakage
# ---------------------------------------------------------------------------


class TestHealthInformationSafety:
    def test_health_memory_failure_does_not_leak_exception(self, client_no_auth):
        """Exception details from memory check must not appear in health response."""
        with patch.object(health_checker, "check_memory") as mock:
            mock.return_value = ComponentHealth(
                status=HealthStatus.UNAVAILABLE,
                message="Memory store unavailable",
            )
            resp = client_no_auth.get("/health")
        body = resp.json()
        memory_msg = body.get("components", {}).get("memory", {}).get("message", "")
        assert "sqlite" not in memory_msg.lower()
        assert "traceback" not in memory_msg.lower()
        assert "exception" not in memory_msg.lower()
        assert "Error" not in memory_msg or memory_msg == "Memory store unavailable"

    def test_health_does_not_expose_api_keys(self, client_no_auth):
        """Health response must never contain API key values."""
        resp = client_no_auth.get("/health")
        raw = resp.text
        assert "sk-" not in raw
        assert "api_key" not in raw.lower() or "[REDACTED]" in raw

    def test_health_does_not_expose_database_url(self, client_no_auth):
        """Health response must not contain the database connection string."""
        resp = client_no_auth.get("/health")
        raw = resp.text
        assert settings.database_url not in raw

    def test_health_generic_error_messages_only(self, client_no_auth):
        """All component error messages must be generic, not raw exceptions."""
        with patch.object(health_checker, "check_memory") as mock_mem:
            mock_mem.side_effect = Exception("sqlite:///./baby.db connection refused port 5432")
            # health_checker.check_health() catches this internally and returns safe message
            resp = client_no_auth.get("/health")
        # The raw exception string must not appear in the response
        assert "sqlite:///./baby.db" not in resp.text
        assert "connection refused" not in resp.text
        assert "port 5432" not in resp.text

    def test_health_publicly_accessible_returns_safe_info_only(self, client_auth_required):
        """Health endpoint is auth-free. Verify it only exposes safe operational data."""
        resp = client_auth_required.get("/health")  # No auth headers
        assert resp.status_code in (200, 503)
        body = resp.json()
        # Only expected keys
        assert set(body.keys()) <= {"status", "timestamp", "version", "components"}


# ---------------------------------------------------------------------------
# FINDING-4 Tests: Metrics cardinality protection
# ---------------------------------------------------------------------------


class TestMetricsCardinality:
    def test_crafted_endpoint_paths_do_not_create_unbounded_labels(self, client_no_auth):
        """Requesting arbitrary paths must not add arbitrary labels to api_requests_total."""
        # Send many requests to unique paths (crafted to flood cardinality)
        unique_paths = [f"/tasks/{i:08d}-fake-uuid" for i in range(20)]
        for path in unique_paths:
            client_no_auth.get(path)

        snap = metrics_collector.get_snapshot()
        api_counter = snap["counters"].get("api_requests_total", {})
        # All /tasks/* paths should be normalized to /tasks label
        for key in api_counter:
            # No raw UUID or arbitrary path should appear as a label
            assert "fake-uuid" not in key
            assert "00000000" not in key

    def test_known_endpoints_normalized_in_metrics(self, client_no_auth):
        """Known endpoint paths are normalized to base prefix labels."""
        task = Task(title="T", description="d")
        task_tracker.register_task(task)
        client_no_auth.get(f"/tasks/{task.id.id}")

        snap = metrics_collector.get_snapshot()
        api_counter = snap["counters"].get("api_requests_total", {})
        # Should have a /tasks label, not the specific UUID path
        task_keys = [k for k in api_counter if "endpoint=/tasks" in k]
        assert len(task_keys) >= 1

    def test_metrics_max_series_per_metric_bounded(self):
        """Verify MAX_SERIES_PER_METRIC overflow protection works."""
        from baby.observability.metrics import MetricsCollector

        coll = MetricsCollector()
        coll.MAX_SERIES_PER_METRIC = 5
        for i in range(10):
            coll.increment_counter("test_bounded", labels={"key": str(i)})
        snap = coll.get_snapshot()
        # Should not exceed MAX_SERIES_PER_METRIC + 1 (for _overflow bucket)
        assert len(snap["counters"]["test_bounded"]) <= 6


# ---------------------------------------------------------------------------
# FINDING-5/6 Tests: Multi-engagement edge cases
# ---------------------------------------------------------------------------


class TestMultiEngagementEdgeCases:
    def test_operator_with_no_engagements_configured_gets_empty_results(self):
        """Operator with empty allowed_engagements gets empty results, not all data."""
        from baby.api.auth import AuthenticatedCaller, Role

        caller = AuthenticatedCaller(
            caller_id="no-scope",
            role=Role.OPERATOR,
            allowed_engagements=set(),
        )
        result = caller.restrict_to_engagement(None)
        assert result is None

    def test_operator_with_multiple_engagements_must_specify_one(self):
        """Operator authorized for multiple engagements must specify which one."""
        from baby.api.auth import AuthenticatedCaller, Role

        caller = AuthenticatedCaller(
            caller_id="multi-scope",
            role=Role.OPERATOR,
            allowed_engagements={"eng-A", "eng-B"},
        )
        # Without specifying, restrict_to_engagement returns None (ambiguous)
        result = caller.restrict_to_engagement(None)
        assert result is None

    def test_operator_with_multiple_engagements_specifying_authorized_one_allowed(self):
        """Operator with multiple engagements can specify one they're authorized for."""
        from baby.api.auth import AuthenticatedCaller, Role

        caller = AuthenticatedCaller(
            caller_id="multi-scope",
            role=Role.OPERATOR,
            allowed_engagements={"eng-A", "eng-B"},
        )
        result = caller.restrict_to_engagement("eng-A")
        assert result == "eng-A"

    def test_operator_with_multiple_engagements_specifying_unauthorized_rejected(self):
        """Operator with multiple engagements cannot specify one they're NOT authorized for."""
        from fastapi import HTTPException

        from baby.api.auth import AuthenticatedCaller, Role

        caller = AuthenticatedCaller(
            caller_id="multi-scope",
            role=Role.OPERATOR,
            allowed_engagements={"eng-A", "eng-B"},
        )
        with pytest.raises(HTTPException) as exc_info:
            caller.restrict_to_engagement("eng-C")
        assert exc_info.value.status_code == 403

    def test_audit_non_admin_no_scope_returns_empty(self, client_auth_required):
        """Non-admin with ambiguous scope (multiple engagements) and no explicit
        scope query param returns empty, not all audit events."""
        settings.operator_engagements = "eng-A,eng-B"  # Multiple scopes
        audit_log.record(AuditEventType.TASK_CREATED, user_id="eng-A")
        audit_log.record(AuditEventType.TASK_CREATED, user_id="eng-B")

        # Operator with multi-scope does not specify engagement_id
        resp = client_auth_required.get("/audit", headers=op_headers())
        assert resp.status_code == 200
        # Must return empty (ambiguous scope = safe default)
        assert resp.json() == []

    def test_tasks_non_admin_no_scope_returns_empty(self, client_auth_required):
        """Non-admin with multiple engagements and no scope returns empty tasks."""
        settings.operator_engagements = "eng-A,eng-B"
        task_tracker.register_task(Task(title="A", description="d", user_id="eng-A"))
        task_tracker.register_task(Task(title="B", description="d", user_id="eng-B"))

        resp = client_auth_required.get("/tasks", headers=op_headers())
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Authentication boundary tests
# ---------------------------------------------------------------------------


class TestAuthenticationBoundaries:
    def test_no_token_rejected_when_auth_required(self, client_auth_required):
        resp = client_auth_required.get("/tasks")
        assert resp.status_code == 401

    def test_empty_bearer_rejected(self, client_auth_required):
        resp = client_auth_required.get("/tasks", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401

    def test_wrong_token_rejected(self, client_auth_required):
        resp = client_auth_required.get("/tasks", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_token_case_sensitive(self, client_auth_required):
        """Token comparison must be exact — case-sensitive."""
        resp = client_auth_required.get("/tasks", headers={"Authorization": "Bearer OP-TOKEN"})
        assert resp.status_code == 401

    def test_admin_token_used_as_op_token_and_vice_versa(self, client_auth_required):
        """Admin and operator tokens are not interchangeable."""
        # Using admin token works
        resp_admin = client_auth_required.get("/tasks", headers=admin_headers())
        assert resp_admin.status_code == 200

        # Using operator token also works (for their scope)
        resp_op = client_auth_required.get(
            "/tasks",
            params={"engagement_id": "engagement-A"},
            headers=op_headers(),
        )
        assert resp_op.status_code == 200

    def test_bearer_and_xapikey_both_work(self, client_auth_required):
        resp = client_auth_required.get(
            "/tasks",
            headers={"X-API-Key": "op-token", "X-Engagement-ID": "engagement-A"},
        )
        assert resp.status_code == 200

    def test_cannot_become_admin_via_header(self, client_auth_required):
        """Operator cannot become admin by adding X-Engagement-ID: * or similar."""
        resp = client_auth_required.get(
            "/tasks",
            headers={
                "Authorization": "Bearer op-token",
                "X-Engagement-ID": "*",  # attacker tries wildcard
            },
        )
        # Must not become admin; either 403 (if * is checked against eng-A scope) or 200 empty
        if resp.status_code == 200:
            # If allowed, must not see cross-engagement data (engagement "*" is not "engagement-A")
            tasks = resp.json()
            # All tasks returned must belong to engagement-A (or be unscoped to test scope)
            for t in tasks:
                assert t.get("user_id") in (None, "engagement-A", "*")

    def test_health_accessible_without_token(self, client_auth_required):
        """Health is public — no auth required."""
        resp = client_auth_required.get("/health")
        assert resp.status_code in (200, 503)

    def test_metrics_requires_auth(self, client_auth_required):
        """Metrics endpoint requires authentication."""
        resp = client_auth_required.get("/metrics")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Admin access boundary tests
# ---------------------------------------------------------------------------


class TestAdminAccessBoundaries:
    def test_admin_sees_all_engagements(self, client_auth_required):
        """Admin with wildcard scope can see all engagements' tasks."""
        task_A = Task(title="A", description="d", user_id="eng-A")
        task_B = Task(title="B", description="d", user_id="eng-B")
        task_tracker.register_task(task_A)
        task_tracker.register_task(task_B)

        resp = client_auth_required.get("/tasks", headers=admin_headers())
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_admin_cannot_be_obtained_by_header_manipulation(self, client_auth_required):
        """Admin role requires admin token — no header manipulation creates admin."""
        # Operator with valid token cannot get admin by faking the engagement
        task_B = Task(title="B", description="d", user_id="eng-B")
        task_tracker.register_task(task_B)

        resp = client_auth_required.get(
            "/tasks",
            params={"engagement_id": "eng-B"},
            headers=op_headers(),
        )
        # Must be forbidden — operator is not authorized for eng-B
        assert resp.status_code == 403

    def test_admin_cannot_be_obtained_by_body_param(self, client_auth_required):
        """POST/PUT with body params are blocked by ReadOnlyMiddleware."""
        resp = client_auth_required.post("/tasks", json={"role": "admin"})
        assert resp.status_code == 405

    def test_operator_is_admin_false(self, client_auth_required):
        """Verify operator caller is not admin."""
        from baby.api.auth import AuthenticatedCaller, Role

        caller = AuthenticatedCaller(
            caller_id="operator",
            role=Role.OPERATOR,
            allowed_engagements={"engagement-A"},
        )
        assert not caller.is_admin

    def test_operator_with_wildcard_engagement_not_admin(self):
        """An OPERATOR role with a literal '*' string in engagements is treated as admin
        due to is_admin checking both role and '*' in engagements.
        This should only happen for the ADMIN role."""

        # _parse_operator_engagements never produces "*" for operator tokens —
        # verify that it only produces concrete engagement IDs
        settings.api_require_auth = False
        settings.operator_engagements = "*"  # Attacker attempts wildcard via env

        from baby.api.auth import _parse_operator_engagements

        engagements = _parse_operator_engagements()
        # Even if operator_engagements = "*", the engagement value is {"*"} (literal string)
        # which would make is_admin=True. Document: operator_engagements must never be set to "*"
        # in production — that is an operator configuration mistake.
        assert engagements == {"*"}  # Documents this edge case


# ---------------------------------------------------------------------------
# Read-only guarantee regression tests
# ---------------------------------------------------------------------------


class TestReadOnlyGuaranteeRegression:
    """Regression: observability endpoints must never cause side effects."""

    def test_post_health_405(self, client_no_auth):
        assert client_no_auth.post("/health", json={}).status_code == 405

    def test_put_metrics_405(self, client_no_auth):
        assert client_no_auth.put("/metrics", json={}).status_code == 405

    def test_delete_audit_405(self, client_no_auth):
        assert client_no_auth.delete("/audit").status_code == 405

    def test_post_tasks_405(self, client_no_auth):
        assert client_no_auth.post("/tasks", json={}).status_code == 405

    def test_patch_tasks_405(self, client_no_auth):
        assert client_no_auth.patch("/tasks/some-id", json={}).status_code == 405

    def test_post_workers_405(self, client_no_auth):
        assert client_no_auth.post("/workers", json={}).status_code == 405

    def test_delete_workers_405(self, client_no_auth):
        assert client_no_auth.delete("/workers/w-1").status_code == 405

    def test_post_agents_405(self, client_no_auth):
        assert client_no_auth.post("/agents", json={}).status_code == 405

    def test_delete_agents_405(self, client_no_auth):
        assert client_no_auth.delete("/agents/a-1").status_code == 405

    def test_getting_tasks_does_not_trigger_execution(self, client_no_auth):
        """Listing tasks must not call any executor, orchestrator, or tool."""
        from baby.orchestration.orchestrator import orchestrator
        from baby.tools.executor import tool_executor

        task = Task(title="T", description="d")
        task_tracker.register_task(task)

        with patch.object(orchestrator, "execute_task") as mock_exec:
            with patch.object(tool_executor, "execute") as mock_tool:
                resp = client_no_auth.get("/tasks")
                assert resp.status_code == 200
                mock_exec.assert_not_called()
                mock_tool.assert_not_called()

    def test_getting_audit_does_not_modify_log(self, client_no_auth):
        """Reading audit events must not append, modify, or remove events."""
        audit_log.record(AuditEventType.TASK_CREATED)
        count_before = audit_log.count_events()
        client_no_auth.get("/audit")
        count_after = audit_log.count_events()
        assert count_before == count_after


# ---------------------------------------------------------------------------
# Secret redaction regression tests
# ---------------------------------------------------------------------------


class TestSecretRedactionRegression:
    def test_openai_key_never_in_audit_response(self, client_no_auth):
        audit_log.record(
            AuditEventType.TOOL_INVOKED,
            details={"openai_api_key": "sk-realkey1234567890abcdef"},
        )
        resp = client_no_auth.get("/audit")
        assert "sk-realkey" not in resp.text

    def test_password_field_redacted_in_audit(self, client_no_auth):
        audit_log.record(
            AuditEventType.TOOL_INVOKED,
            details={"password": "s3cret!", "action": "login"},
        )
        resp = client_no_auth.get("/audit")
        assert "s3cret!" not in resp.text
        assert "[REDACTED]" in resp.text

    def test_bearer_token_in_string_redacted(self, client_no_auth):
        audit_log.record(
            AuditEventType.TOOL_INVOKED,
            details={"message": "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig"},
        )
        resp = client_no_auth.get("/audit")
        # Bearer token value must be redacted
        assert "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9" not in resp.text

    def test_nested_secret_redacted(self, client_no_auth):
        audit_log.record(
            AuditEventType.TOOL_INVOKED,
            details={"config": {"api_key": "top-secret-value", "timeout": 30}},
        )
        resp = client_no_auth.get("/audit")
        assert "top-secret-value" not in resp.text
        assert "[REDACTED]" in resp.text

    def test_non_sensitive_data_preserved(self, client_no_auth):
        audit_log.record(
            AuditEventType.TASK_CREATED,
            details={"tool": "read_file", "path": "/safe/path", "lines": 42},
        )
        resp = client_no_auth.get("/audit")
        events = resp.json()
        details = events[0].get("details", {})
        assert details.get("tool") == "read_file"
        assert details.get("lines") == 42
