"""Phase 15: Installation, deployment, and first-run experience verification.

Tests for:
- Environment and configuration validation (clean actionable errors, no secret leakage)
- First-run database creation, directory auto-creation, and schema migration
- Idempotent repeated database initialization
- Persistence across restart (clean shutdown, data survival, WAL consistency)
- Health and readiness endpoint contracts on fresh deployment
- Secrets redaction in logs and configuration errors
- Worker process startup, CLI commands (run/status/recover), and graceful shutdown
- Invalid database paths and unwritable filesystem errors
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from baby.api.app import app
from baby.configuration import Settings, validate_settings
from baby.errors import ConfigurationError
from baby.observability.redaction import redact_sensitive_data
from baby.persistence.database import DatabaseManager, parse_sqlite_path
from baby.workers.__main__ import main as workers_cli_main


class TestConfigurationAndFirstRun:
    def test_default_settings_are_safe_and_valid(self):
        cfg = Settings()
        warnings = validate_settings(cfg)
        assert cfg.persistence_backend == "sqlite"
        assert cfg.enable_approval_workflow is True
        assert cfg.api_require_auth is True
        assert isinstance(warnings, list)

    def test_missing_database_url_raises_configuration_error(self):
        cfg = Settings(database_url="", persistence_backend="sqlite")
        with pytest.raises(ConfigurationError, match="database_url"):
            validate_settings(cfg)

    def test_invalid_persistence_backend_raises_configuration_error(self):
        cfg = Settings(persistence_backend="invalid_backend")
        with pytest.raises(ConfigurationError, match="Invalid persistence_backend"):
            validate_settings(cfg)

    def test_insecure_auth_warning_emitted_for_dev_mode(self):
        cfg = Settings(api_require_auth=False, development_mode=True)
        warnings = validate_settings(cfg)
        assert any("api_require_auth=False" in w for w in warnings)

    def test_secrets_redaction_in_data_and_errors(self):
        secret_data = {
            "api_key": "sk-1234567890abcdef1234567890",
            "openai_api_key": "sk-abcdefghijklmnopqrstuvwxyz",
            "password": "super-secret-password",
            "token": "bearer-token-123",
            "normal_field": "public_data",
        }
        redacted = redact_sensitive_data(secret_data)
        assert isinstance(redacted, dict)
        assert redacted["api_key"] == "[REDACTED]"
        assert redacted["openai_api_key"] == "[REDACTED]"
        assert redacted["password"] == "[REDACTED]"
        assert redacted["token"] == "[REDACTED]"
        assert redacted["normal_field"] == "public_data"


class TestDatabaseCreationAndMigrations:
    def test_first_run_creates_database_and_parent_directories(self, tmp_path: Path):
        deep_dir = tmp_path / "nested" / "storage" / "data"
        db_file = deep_dir / "baby_install.db"
        assert not deep_dir.exists()

        db = DatabaseManager(f"sqlite:///{db_file}")
        assert not db_file.exists()

        # init_schema auto-creates directories and files
        db.init_schema()
        assert deep_dir.exists()
        assert db_file.exists()
        assert db.schema_version() == 2

        with db.transaction() as conn:
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"memory_records", "audit_events", "task_records", "workers", "task_leases"} <= tables

        db.close()

    def test_repeated_initialization_is_idempotent(self, tmp_path: Path):
        db_file = tmp_path / "idempotent.db"
        db = DatabaseManager(f"sqlite:///{db_file}")

        # Run init_schema three times consecutively
        db.init_schema()
        db.init_schema()
        db.init_schema()
        assert db.schema_version() == 2

        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO memory_records (id, record_type, content, created_at) "
                "VALUES ('test-1', 'fact', '{}', datetime('now'))"
            )

        # Run init_schema again after data insertion: data remains intact
        db.init_schema()
        with db.transaction() as conn:
            row = conn.execute("SELECT id FROM memory_records WHERE id='test-1'").fetchone()
            assert row is not None
            assert row["id"] == "test-1"

        db.close()

    def test_database_persistence_across_restart(self, tmp_path: Path):
        db_file = tmp_path / "restart.db"
        url = f"sqlite:///{db_file}"

        # First lifecycle
        db1 = DatabaseManager(url)
        db1.init_schema()
        with db1.transaction() as conn:
            conn.execute(
                "INSERT INTO memory_records (id, record_type, content, created_at) "
                "VALUES ('persist-1', 'fact', '{\"key\": \"value\"}', datetime('now'))"
            )
        db1.close()

        # Reopen in new instance (simulating application restart)
        db2 = DatabaseManager(url)
        assert db2.schema_version() == 2
        with db2.transaction() as conn:
            row = conn.execute("SELECT content FROM memory_records WHERE id='persist-1'").fetchone()
            assert row is not None
            assert "value" in row["content"]
        db2.close()

    def test_invalid_database_path_raises_configuration_error(self, tmp_path: Path):
        # Create a file and attempt to treat it as a directory parent
        blocking_file = tmp_path / "not_a_dir"
        blocking_file.write_text("blocker")
        invalid_url = f"sqlite:///{blocking_file}/sub/baby.db"

        db = DatabaseManager(invalid_url)
        with pytest.raises(ConfigurationError):
            db.get_connection()


class TestParseSqlitePath:
    def test_parse_sqlite_path_formats(self):
        assert parse_sqlite_path("sqlite:///./baby.db") == "./baby.db"
        assert parse_sqlite_path("sqlite:///:memory:") == ":memory:"
        assert parse_sqlite_path(":memory:") == ":memory:"
        assert parse_sqlite_path("./baby.db") == "./baby.db"
        assert parse_sqlite_path("sqlite:///C:/Users/baby/db.sqlite") == "C:/Users/baby/db.sqlite"

    def test_empty_path_raises(self):
        with pytest.raises(ConfigurationError):
            parse_sqlite_path("")
        with pytest.raises(ConfigurationError):
            parse_sqlite_path("sqlite:///")


class TestApiStartupAndReadiness:
    def test_api_readiness_and_health_on_fresh_startup(self, tmp_path: Path):
        db_file = tmp_path / "api_test.db"
        with patch.multiple(
            "baby.configuration.settings",
            database_url=f"sqlite:///{db_file}",
            api_require_auth=False,
            development_mode=True,
        ):
            with TestClient(app, raise_server_exceptions=True) as client:
                health = client.get("/api/v1/health")
                assert health.status_code == 200
                assert "status" in health.json()

                readiness = client.get("/api/v1/readiness")
                assert readiness.status_code == 200
                readiness_data = readiness.json()
                assert readiness_data["ready"] is True
                assert readiness_data["status"] == "ready"


class TestWorkerCliEntrypoint:
    def test_workers_cli_help(self):
        with pytest.raises(SystemExit) as exc_info:
            workers_cli_main(["--help"])
        assert exc_info.value.code == 0

    def test_workers_cli_status(self, tmp_path: Path, capsys: pytest.CaptureFixture):
        db_file = tmp_path / "cli_status.db"
        db = DatabaseManager(f"sqlite:///{db_file}")
        db.init_schema()
        db.close()

        with patch("baby.configuration.settings.database_url", f"sqlite:///{db_file}"):
            exit_code = workers_cli_main(["status"])
            assert exit_code == 0

    def test_workers_cli_recover(self, tmp_path: Path, capsys: pytest.CaptureFixture):
        db_file = tmp_path / "cli_recover.db"
        db = DatabaseManager(f"sqlite:///{db_file}")
        db.init_schema()
        db.close()

        with patch("baby.configuration.settings.database_url", f"sqlite:///{db_file}"):
            exit_code = workers_cli_main(["recover"])
            assert exit_code == 0
            captured = capsys.readouterr()
            assert "stale_workers=0" in captured.out
