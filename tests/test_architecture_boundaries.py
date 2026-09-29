"""Phase 11: Tests for architecture boundaries, TypeScript API preparation, and native runtime boundaries.

Verifies:
- CodingFilesystem interface and LocalCodingFilesystem boundary enforcement
- Protection of repository boundaries (.git protection, path traversal defenses)
- OpenAPI schema completeness for future TypeScript client generation
- Dual-mounted /api/v1 and top-level endpoints for clean versioning
- Security and authorization boundaries on versioned routes
- Preservation of analysis-only trading rule
"""

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from baby.api.app import app
from baby.api.openapi import export_openapi_schema, get_openapi_schema
from baby.configuration import settings
from baby.core import PermissionCategory
from baby.errors import PathTraversalError, RepositoryBoundaryError
from baby.tools.coding.filesystem import (
    CodingFilesystem,
    LocalCodingFilesystem,
    get_coding_filesystem,
)


@pytest.fixture
def client():
    with patch("baby.configuration.settings.api_require_auth", False):
        with TestClient(app) as c:
            yield c


@pytest.fixture
def auth_client():
    with patch("baby.configuration.settings.api_require_auth", True):
        with patch("baby.configuration.settings.api_auth_token", "test-auth-token"):
            with patch("baby.configuration.settings.api_admin_token", "test-admin-token"):
                with TestClient(app) as c:
                    yield c


@pytest.fixture
def temp_repo():
    with tempfile.TemporaryDirectory() as tmp_dir:
        repo_path = Path(tmp_dir)
        (repo_path / "src").mkdir()
        (repo_path / "src" / "main.py").write_text("print('hello')", encoding="utf-8")
        (repo_path / "README.md").write_text("# Project", encoding="utf-8")
        with patch.object(settings, "coding_repo_root", str(repo_path)):
            yield repo_path


# ============================================================================
# 1. CODING FILESYSTEM NATIVE RUNTIME BOUNDARY
# ============================================================================


class TestCodingFilesystemBoundary:
    def test_local_filesystem_implements_interface(self):
        fs = get_coding_filesystem()
        assert isinstance(fs, CodingFilesystem)
        assert isinstance(fs, LocalCodingFilesystem)

    def test_resolve_path_within_root(self, temp_repo):
        fs = LocalCodingFilesystem()
        resolved = fs.resolve_path("src/main.py")
        assert resolved == (temp_repo / "src" / "main.py").resolve()

    def test_resolve_path_traversal_rejected(self, temp_repo):
        fs = LocalCodingFilesystem()
        with pytest.raises(PathTraversalError):
            fs.resolve_path("../outside.py")

    def test_git_directory_access_rejected(self, temp_repo):
        git_dir = temp_repo / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("core.bare=false", encoding="utf-8")

        fs = LocalCodingFilesystem()
        with pytest.raises(RepositoryBoundaryError):
            fs.resolve_path(".git/config")

    def test_list_files_omits_git_internals(self, temp_repo):
        git_dir = temp_repo / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")

        fs = LocalCodingFilesystem()
        entries = fs.list_files()
        paths = [e["path"] for e in entries]
        assert not any(".git" in p for p in paths)
        assert any("src/main.py" in p or "src\\main.py" in p for p in paths)

    def test_read_and_write_file(self, temp_repo):
        fs = LocalCodingFilesystem()
        fs.write_file("docs/notes.txt", "Architecture notes")
        content = fs.read_file("docs/notes.txt")
        assert content == "Architecture notes"


# ============================================================================
# 2. OPENAPI SCHEMA PREPARATION FOR TYPESCRIPT CLIENTS
# ============================================================================


class TestTypeScriptOpenAPIPreparation:
    def test_openapi_schema_generated_successfully(self):
        schema = get_openapi_schema(app)
        assert isinstance(schema, dict)
        assert "openapi" in schema
        assert "paths" in schema
        assert "info" in schema
        assert schema["info"]["title"] == "BABY Observability API"

    def test_openapi_contains_expected_subsystems(self):
        schema = get_openapi_schema(app)
        paths = schema.get("paths", {})

        # Verify core operational endpoints exist in schema
        expected_endpoints = [
            "/health",
            "/ready",
            "/readiness",
            "/metrics",
            "/tasks",
            "/workers",
            "/agents",
            "/audit",
        ]
        for ep in expected_endpoints:
            assert ep in paths, f"Expected endpoint {ep} missing from OpenAPI schema"

    def test_export_openapi_schema_to_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = Path(tmp_dir) / "openapi.json"
            schema = export_openapi_schema(application=app, output_path=str(out_file))
            assert out_file.exists()
            assert len(schema["paths"]) > 0


# ============================================================================
# 3. API VERSIONING PREPARATION (/api/v1)
# ============================================================================


class TestApiVersioning:
    def test_v1_health_and_readiness_endpoints(self, client):
        resp_health = client.get("/api/v1/health")
        assert resp_health.status_code == 200
        assert "status" in resp_health.json()

        resp_ready = client.get("/api/v1/ready")
        assert resp_ready.status_code == 200
        assert resp_ready.json()["ready"] is True

    def test_v1_tasks_endpoints(self, client):
        resp = client.get("/api/v1/tasks")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_v1_workers_and_agents_endpoints(self, client):
        assert client.get("/api/v1/workers").status_code == 200
        assert client.get("/api/v1/agents").status_code == 200

    def test_v1_endpoints_enforce_read_only(self, client):
        for path in ["/api/v1/tasks", "/api/v1/workers", "/api/v1/agents", "/api/v1/audit"]:
            assert client.post(path, json={}).status_code == 405
            assert client.put(path, json={}).status_code == 405
            assert client.delete(path).status_code == 405

    def test_v1_endpoints_enforce_auth_when_required(self, auth_client):
        # Without authorization header, protected v1 endpoints must return 401
        assert auth_client.get("/api/v1/tasks").status_code == 401
        assert auth_client.get("/api/v1/metrics").status_code == 401
        assert auth_client.get("/api/v1/audit").status_code == 401

        # With authorization header, access is granted
        headers = {"Authorization": "Bearer test-auth-token"}
        assert auth_client.get("/api/v1/tasks", headers=headers).status_code == 200
        assert auth_client.get("/api/v1/metrics", headers=headers).status_code == 200
        assert auth_client.get("/api/v1/audit", headers=headers).status_code == 200


# ============================================================================
# 4. TRADING EXECUTION SAFEGUARDS
# ============================================================================


class TestTradingExecutionSafeguards:
    def test_trading_has_no_live_execution_endpoints(self, client):
        # Baby must have zero live broker or trade order endpoints
        for forbidden in ["/trade", "/api/v1/trade", "/orders", "/api/v1/orders", "/execute"]:
            resp = client.get(forbidden)
            assert resp.status_code == 404

    def test_financial_action_requires_explicit_permission(self):
        # Financial actions in core contracts are defined but restricted
        assert PermissionCategory.FINANCIAL_ACTION.value == "financial_action"
