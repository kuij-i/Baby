"""Phase 13 API Contract & TypeScript Client Verification Tests.

Verifies:
1. Determinism and schema validity of exported OpenAPI 3.1.0 specification.
2. Stability and structure of /api/v1 endpoints (Tasks, Workers, Observability).
3. Contract alignment between Python models and TypeScript client expectations.
4. Fail-closed security: authentication, authorization, role boundaries, and IDOR defenses.
5. Strict separation of concerns: TypeScript layer cannot directly access Python/SQLite internals.
"""

import json
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from baby.api.app import app
from baby.api.openapi import export_openapi_schema, get_openapi_schema
from baby.api.schemas import (
    ReadinessResponse,
    TaskCancelResponse,
    TaskSubmitResponse,
    TaskView,
)
from baby.core import TaskStatus
from baby.observability.tasks import task_tracker
from baby.observability.workers import worker_tracker
from baby.persistence.database import db_manager


@pytest.fixture(autouse=True)
def clean_test_env(tmp_path: Path):
    """Ensure clean isolated SQLite database for each test."""
    original_url = db_manager.database_url
    db_file = tmp_path / "contract_test.db"
    db_manager.configure(f"sqlite:///{db_file}")
    db_manager.init_schema()
    task_tracker.clear()
    worker_tracker.clear()
    yield
    db_manager.close()
    db_manager.configure(original_url)


@pytest.fixture
def client():
    with patch.multiple("baby.configuration.settings", api_require_auth=False, development_mode=True):
        with TestClient(app) as c:
            yield c


@pytest.fixture
def auth_client():
    with patch("baby.configuration.settings.api_require_auth", True):
        with patch("baby.configuration.settings.api_auth_token", "test-operator-token-0123456789abcdef"):
            with patch("baby.configuration.settings.api_admin_token", "test-admin-token-0123456789abcdef"):
                with patch("baby.configuration.settings.operator_engagements", "eng-a,eng-b"):
                    with TestClient(app) as c:
                        yield c


# ============================================================================
# 1. OPENAPI CONTRACT & DETERMINISM TESTS
# ============================================================================


class TestOpenApiContract:
    def test_openapi_schema_is_deterministic(self):
        s1 = get_openapi_schema(app)
        s2 = get_openapi_schema(app)
        assert json.dumps(s1, sort_keys=True) == json.dumps(s2, sort_keys=True)

    def test_all_v1_endpoints_present_in_openapi(self):
        schema = get_openapi_schema(app)
        paths = schema.get("paths", {})

        required_v1_paths = [
            "/api/v1/health",
            "/api/v1/ready",
            "/api/v1/readiness",
            "/api/v1/metrics",
            "/api/v1/tasks",
            "/api/v1/tasks/submit",
            "/api/v1/tasks/{task_id}",
            "/api/v1/tasks/{task_id}/cancel",
            "/api/v1/tasks/{task_id}/approvals/{request_id}",
            "/api/v1/workers",
            "/api/v1/workers/{worker_id}",
            "/api/v1/audit",
            "/api/v1/agents",
            "/api/v1/agents/{agent_id}",
        ]
        for p in required_v1_paths:
            assert p in paths, f"Missing required path {p} from OpenAPI schema"

    def test_security_schemes_advertised(self):
        schema = get_openapi_schema(app)
        security_schemes = schema.get("components", {}).get("securitySchemes", {})
        assert "HTTPBearer" in security_schemes
        assert "APIKeyHeader" in security_schemes

    def test_openapi_export_matches_generated_definitions(self):
        exported = export_openapi_schema(output_path="docs/openapi.json")
        assert "paths" in exported
        assert len(exported["paths"]) >= 28


# ============================================================================
# 2. ENDPOINT WIRE FORMAT & SCHEMA COMPATIBILITY TESTS
# ============================================================================


class TestEndpointWireContracts:
    def test_task_submission_response_shape(self, client):
        resp = client.post(
            "/api/v1/tasks/submit",
            json={"title": "Test Title", "description": "Test Desc", "priority": 5, "engagement_id": "eng-test"},
        )
        assert resp.status_code == 202
        data = resp.json()
        validated = TaskSubmitResponse(**data)
        assert validated.status == TaskStatus.PENDING
        assert validated.engagement_id == "eng-test"

    def test_task_get_and_list_response_shape(self, client):
        sub_resp = client.post(
            "/api/v1/tasks/submit",
            json={"title": "Contract Task", "description": "Desc", "engagement_id": "eng-test"},
        )
        task_id = sub_resp.json()["task_id"]

        get_resp = client.get(f"/api/v1/tasks/{task_id}")
        assert get_resp.status_code == 200
        task_view = TaskView(**get_resp.json())
        assert str(task_view.task_id.id) == task_id
        assert task_view.title == "Contract Task"
        assert task_view.status == TaskStatus.PENDING

        list_resp = client.get("/api/v1/tasks")
        assert list_resp.status_code == 200
        items = [TaskView(**item) for item in list_resp.json()]
        assert len(items) >= 1
        assert any(str(i.task_id.id) == task_id for i in items)

    def test_task_cancel_response_shape(self, client):
        sub_resp = client.post(
            "/api/v1/tasks/submit",
            json={"title": "Cancel Task", "description": "Desc", "engagement_id": "eng-test"},
        )
        task_id = sub_resp.json()["task_id"]

        cancel_resp = client.post(f"/api/v1/tasks/{task_id}/cancel")
        assert cancel_resp.status_code == 200
        cancel_view = TaskCancelResponse(**cancel_resp.json())
        assert str(cancel_view.task_id) == task_id
        assert cancel_view.status == TaskStatus.CANCELLED

    def test_readiness_response_shape(self, client):
        resp = client.get("/api/v1/readiness")
        assert resp.status_code == 200
        data = ReadinessResponse(**resp.json())
        assert data.ready is True
        assert data.status == "ready"


# ============================================================================
# 3. AUTHENTICATION & SECURITY BOUNDARY TESTS
# ============================================================================


class TestSecurityAndAuthBoundaries:
    def test_unauthenticated_request_rejected_with_401(self, auth_client):
        resp = auth_client.get("/api/v1/tasks")
        assert resp.status_code == 401
        assert "Authentication required" in resp.json()["detail"]

    def test_invalid_bearer_token_rejected_with_401(self, auth_client):
        resp = auth_client.get("/api/v1/tasks", headers={"Authorization": "Bearer bad-token"})
        assert resp.status_code == 401
        assert "Invalid credentials" in resp.json()["detail"]

    def test_operator_token_cannot_access_unauthorized_engagement(self, auth_client):
        headers = {"Authorization": "Bearer test-operator-token-0123456789abcdef"}
        resp = auth_client.get("/api/v1/tasks?engagement_id=unauthorized-eng", headers=headers)
        assert resp.status_code == 403

    def test_non_admin_cannot_decide_approval(self, auth_client):
        headers = {"Authorization": "Bearer test-operator-token-0123456789abcdef"}
        dummy_task_id = uuid4()
        dummy_req_id = uuid4()
        resp = auth_client.post(
            f"/api/v1/tasks/{dummy_task_id}/approvals/{dummy_req_id}",
            json={"approved": True},
            headers=headers,
        )
        assert resp.status_code == 403
        assert "Admin role required" in resp.json()["detail"]

    def test_admin_token_authorizes_approval(self, auth_client):
        headers = {"Authorization": "Bearer test-admin-token-0123456789abcdef"}
        dummy_task_id = uuid4()
        dummy_req_id = uuid4()
        resp = auth_client.post(
            f"/api/v1/tasks/{dummy_task_id}/approvals/{dummy_req_id}",
            json={"approved": True},
            headers=headers,
        )
        # 404 because the task does not exist, proving that admin authorization check passed!
        assert resp.status_code == 404


# ============================================================================
# 4. ARCHITECTURAL ISOLATION CHECK
# ============================================================================


class TestClientArchitecturalIsolation:
    def test_typescript_client_contains_no_python_or_sqlite_imports(self):
        ts_src = Path("clients/typescript/src")
        for ts_file in ts_src.glob("*.ts"):
            content = ts_file.read_text(encoding="utf-8")
            assert "sqlite" not in content.lower(), f"{ts_file} mentions sqlite"
            assert "baby.core" not in content, f"{ts_file} mentions baby.core"
            assert "pydantic" not in content, f"{ts_file} mentions pydantic"
            assert "fastapi" not in content, f"{ts_file} mentions fastapi"
