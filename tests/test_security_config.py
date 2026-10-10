"""Phase 17 / M1: security configuration hardening.

Covers:
- API credential policy: placeholder, short and identical admin/operator tokens are
  rejected at startup (fail closed, non-secret messages) and never authenticate at runtime.
- Constant-time byte comparison: non-ASCII or malformed credentials yield 401, never 500.
- Explicit development mode: disabling authentication requires development_mode=True;
  api_require_auth=False on its own keeps authentication enforced.
- Coding repository root validation before coding tools are enabled (worker bootstrap and
  supervisor): missing, invalid or unsafe roots (containing Baby's database or .env) fail.
"""

import secrets
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from baby.agents import agent_registry
from baby.api.app import app
from baby.configuration import MIN_API_TOKEN_LENGTH, Settings, api_token_problem, settings, validate_settings
from baby.core import PermissionCategory, PermissionLevel
from baby.errors import ConfigurationError
from baby.permissions import permission_manager
from baby.tools.coding.path_resolver import validate_coding_repo_root
from baby.workers.bootstrap import default_bootstrap
from baby.workers.supervisor import WorkerSupervisor

OPERATOR_TOKEN = secrets.token_hex(24)
ADMIN_TOKEN = secrets.token_hex(24)
PLACEHOLDERS = ["change-me-operator-token", "change-me-admin-token"]


def _cfg(**overrides) -> Settings:
    cfg = Settings()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


# ============================================================================
# A. Credential validation (startup)
# ============================================================================


class TestCredentialPolicy:
    def test_generated_tokens_are_accepted(self):
        validate_settings(_cfg(api_auth_token=OPERATOR_TOKEN, api_admin_token=ADMIN_TOKEN))

    def test_no_tokens_configured_is_still_valid_and_fails_closed_with_warning(self):
        warnings = validate_settings(_cfg(api_auth_token=None, api_admin_token=None))
        assert any("will return 401" in w for w in warnings)

    @pytest.mark.parametrize("field", ["api_auth_token", "api_admin_token"])
    @pytest.mark.parametrize("placeholder", PLACEHOLDERS)
    def test_env_example_placeholders_are_rejected(self, field, placeholder):
        with pytest.raises(ConfigurationError, match=f"{field} is a placeholder"):
            validate_settings(_cfg(**{field: placeholder}))

    @pytest.mark.parametrize(
        "value",
        [
            "CHANGE-ME-ADMIN-TOKEN",
            "changeme" + "x" * 40,
            "please-replace-me-with-a-real-secret-value",
            "placeholder-" + "a" * 40,
            "your-token-goes-here-0123456789abcdef",
        ],
    )
    def test_placeholder_markers_are_rejected_even_when_long(self, value):
        with pytest.raises(ConfigurationError, match="placeholder"):
            validate_settings(_cfg(api_admin_token=value))

    @pytest.mark.parametrize("field", ["api_auth_token", "api_admin_token"])
    def test_short_tokens_are_rejected(self, field):
        short = secrets.token_hex(15)  # 30 characters
        with pytest.raises(ConfigurationError, match=f"{field} must be at least {MIN_API_TOKEN_LENGTH} characters"):
            validate_settings(_cfg(**{field: short}))

    def test_minimum_length_token_is_accepted(self):
        validate_settings(_cfg(api_auth_token="a1" * (MIN_API_TOKEN_LENGTH // 2), api_admin_token=ADMIN_TOKEN))

    def test_whitespace_padded_token_is_rejected(self):
        with pytest.raises(ConfigurationError, match="whitespace"):
            validate_settings(_cfg(api_auth_token=f" {OPERATOR_TOKEN} "))

    def test_identical_admin_and_operator_tokens_rejected_when_auth_required(self):
        with pytest.raises(ConfigurationError, match="must be different"):
            validate_settings(_cfg(api_require_auth=True, api_auth_token=ADMIN_TOKEN, api_admin_token=ADMIN_TOKEN))

    def test_error_messages_never_contain_the_token(self):
        weak = "change-me-but-this-is-actually-my-real-secret-123"
        short = "short-secret-xyz"
        for cfg in (
            _cfg(api_admin_token=weak),
            _cfg(api_auth_token=short),
            _cfg(api_auth_token=ADMIN_TOKEN, api_admin_token=ADMIN_TOKEN),
        ):
            with pytest.raises(ConfigurationError) as exc_info:
                validate_settings(cfg)
            message = str(exc_info.value)
            for secret in (weak, short, ADMIN_TOKEN):
                assert secret not in message

    def test_api_token_problem_treats_unset_as_not_configured(self):
        assert api_token_problem("api_auth_token", None) is None
        assert api_token_problem("api_auth_token", "") is None

    def test_env_example_ships_no_usable_credentials(self):
        env_example = Path(__file__).resolve().parent.parent / ".env.example"
        values = {}
        for line in env_example.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        assert values.get("API_REQUIRE_AUTH") == "true"
        assert values.get("DEVELOPMENT_MODE", "false") == "false"
        for key in ("API_AUTH_TOKEN", "API_ADMIN_TOKEN"):
            assert values.get(key, "") == "", f"{key} must not ship a value in .env.example"

    def test_api_startup_fails_closed_on_placeholder_token(self):
        with patch.multiple(
            settings, api_require_auth=True, api_auth_token=OPERATOR_TOKEN, api_admin_token=PLACEHOLDERS[1]
        ):
            with pytest.raises(ConfigurationError, match="placeholder"):
                with TestClient(app):
                    pass

    def test_readiness_reports_not_ready_for_invalid_credentials(self):
        with patch.multiple(settings, api_require_auth=True, api_auth_token=OPERATOR_TOKEN, api_admin_token="x" * 10):
            resp = TestClient(app, raise_server_exceptions=False).get("/api/v1/readiness")
        assert resp.status_code == 503
        assert "Configuration validation failed" in resp.json()["issues"]


# ============================================================================
# B. Constant-time comparison and malformed input (runtime)
# ============================================================================


@pytest.fixture
def strict_client():
    with patch.multiple(
        settings,
        api_require_auth=True,
        development_mode=False,
        api_auth_token=OPERATOR_TOKEN,
        api_admin_token=ADMIN_TOKEN,
        operator_engagements="default",
    ):
        yield TestClient(app, raise_server_exceptions=False)


class TestTokenComparison:
    def test_valid_tokens_authenticate(self, strict_client):
        assert strict_client.get("/api/v1/tasks", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}).status_code == 200
        assert strict_client.get("/api/v1/tasks", headers={"X-API-Key": OPERATOR_TOKEN}).status_code == 200

    @pytest.mark.parametrize(
        "raw",
        [
            "Bearer caf\xe9".encode("latin-1"),
            "Bearer \U0001f511-key".encode("utf-8"),
            ("Bearer " + ADMIN_TOKEN[:-1] + "\xe9").encode("latin-1"),
        ],
    )
    def test_non_ascii_bearer_token_returns_401(self, strict_client, raw):
        resp = strict_client.get("/api/v1/tasks", headers={"Authorization": raw})
        assert resp.status_code == 401

    def test_non_ascii_api_key_returns_401(self, strict_client):
        resp = strict_client.get("/api/v1/tasks", headers={"X-API-Key": "s\xe9cret".encode("latin-1")})
        assert resp.status_code == 401

    @pytest.mark.parametrize(
        "headers",
        [
            {"Authorization": "Basic Zm9vOmJhcg=="},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer    "},
            {"Authorization": "Token " + ADMIN_TOKEN},
            {"Authorization": "Bearer " + "A" * 10_000},
            {"X-API-Key": "   "},
            {"Authorization": "Bearer " + ADMIN_TOKEN.upper()},
        ],
    )
    def test_malformed_credentials_return_401_not_500(self, strict_client, headers):
        assert strict_client.get("/api/v1/tasks", headers=headers).status_code == 401

    def test_placeholder_token_never_authenticates_at_runtime(self):
        with patch.multiple(
            settings, api_require_auth=True, api_auth_token=OPERATOR_TOKEN, api_admin_token=PLACEHOLDERS[1]
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/v1/tasks", headers={"Authorization": f"Bearer {PLACEHOLDERS[1]}"})
            assert resp.status_code == 401
            # The valid operator token keeps working.
            assert client.get("/api/v1/tasks", headers={"X-API-Key": OPERATOR_TOKEN}).status_code == 200

    def test_short_token_never_authenticates_at_runtime(self):
        with patch.multiple(settings, api_require_auth=True, api_auth_token="op-token", api_admin_token=ADMIN_TOKEN):
            resp = TestClient(app, raise_server_exceptions=False).get(
                "/api/v1/tasks", headers={"X-API-Key": "op-token"}
            )
        assert resp.status_code == 401

    def test_identical_tokens_never_authenticate_at_runtime(self):
        with patch.multiple(settings, api_require_auth=True, api_auth_token=ADMIN_TOKEN, api_admin_token=ADMIN_TOKEN):
            resp = TestClient(app, raise_server_exceptions=False).get(
                "/api/v1/tasks", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}
            )
        assert resp.status_code == 401


# ============================================================================
# C. Explicit development mode
# ============================================================================


class TestDevelopmentMode:
    def test_production_defaults_are_secure(self):
        fresh = Settings()
        assert fresh.api_require_auth is True
        assert fresh.development_mode is False

    def test_disabling_auth_without_development_mode_is_rejected(self):
        with pytest.raises(ConfigurationError, match="development_mode=True"):
            validate_settings(_cfg(api_require_auth=False, development_mode=False))

    def test_disabling_auth_with_development_mode_is_allowed_with_warning(self):
        warnings = validate_settings(_cfg(api_require_auth=False, development_mode=True))
        assert any("development mode" in w and "DO NOT use in production" in w for w in warnings)

    def test_development_mode_does_not_relax_credential_policy(self):
        with pytest.raises(ConfigurationError, match="placeholder"):
            validate_settings(_cfg(api_require_auth=False, development_mode=True, api_admin_token=PLACEHOLDERS[1]))

    def test_auth_disabled_without_development_mode_keeps_authentication_enforced(self):
        with patch.multiple(
            settings,
            api_require_auth=False,
            development_mode=False,
            api_auth_token=OPERATOR_TOKEN,
            api_admin_token=ADMIN_TOKEN,
        ):
            client = TestClient(app, raise_server_exceptions=False)
            assert client.get("/api/v1/tasks").status_code == 401
            assert client.post("/api/v1/tasks/submit", json={"title": "t", "description": "d"}).status_code == 401
            authed = client.get("/api/v1/tasks", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
            assert authed.status_code == 200

    def test_api_startup_rejects_auth_disabled_without_development_mode(self):
        with patch.multiple(settings, api_require_auth=False, development_mode=False):
            with pytest.raises(ConfigurationError, match="development_mode"):
                with TestClient(app):
                    pass

    def test_explicit_development_mode_still_allows_local_unauthenticated_use(self):
        with patch.multiple(settings, api_require_auth=False, development_mode=True):
            assert TestClient(app).get("/api/v1/tasks").status_code == 200


# ============================================================================
# D. Coding repository root validation
# ============================================================================


@pytest.fixture
def layout(tmp_path: Path):
    """Baby's data in tmp/baby (db + .env); a separate project in tmp/project."""
    baby_home = tmp_path / "baby"
    baby_home.mkdir()
    (baby_home / ".env").write_text("API_ADMIN_TOKEN=not-a-real-secret\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    return {
        "root": tmp_path,
        "project": project,
        "db": str(baby_home / "baby.db"),
        "env": str(baby_home / ".env"),
    }


class TestCodingRepoRootValidation:
    def _validate(self, layout, repo_root):
        return validate_coding_repo_root(repo_root=repo_root, database_path=layout["db"], env_file=layout["env"])

    def test_separate_project_directory_is_accepted(self, layout):
        assert self._validate(layout, str(layout["project"])) == layout["project"].resolve()

    def test_project_with_its_own_env_file_is_accepted(self, layout):
        (layout["project"] / ".env").write_text("PROJECT_SETTING=1\n", encoding="utf-8")
        assert self._validate(layout, str(layout["project"])) == layout["project"].resolve()

    def test_missing_root_is_rejected(self, layout):
        with patch.object(settings, "coding_repo_root", None):
            with pytest.raises(ConfigurationError, match="not configured"):
                self._validate(layout, None)

    def test_nonexistent_root_is_rejected(self, layout):
        with pytest.raises(ConfigurationError, match="does not exist"):
            self._validate(layout, str(layout["root"] / "nope"))

    def test_file_root_is_rejected(self, layout):
        file_root = layout["project"] / "file.txt"
        file_root.write_text("x", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="not a directory"):
            self._validate(layout, str(file_root))

    def test_root_containing_baby_database_is_rejected(self, layout):
        with pytest.raises(ConfigurationError, match="database"):
            validate_coding_repo_root(
                repo_root=str(layout["root"]), database_path=layout["db"], env_file=str(layout["project"] / ".env")
            )

    def test_root_containing_baby_env_file_is_rejected(self, layout):
        with pytest.raises(ConfigurationError, match=r"\.env file"):
            validate_coding_repo_root(
                repo_root=str(layout["root"]), database_path=str(layout["project"] / "x.db"), env_file=layout["env"]
            )

    def test_root_equal_to_baby_home_is_rejected(self, layout):
        with pytest.raises(ConfigurationError, match="contains Baby's"):
            self._validate(layout, str(Path(layout["db"]).parent))

    def test_symlinked_root_is_resolved_before_checking(self, layout):
        link = layout["project"] / "baby-link"
        try:
            link.symlink_to(Path(layout["db"]).parent, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported")
        with pytest.raises(ConfigurationError, match="contains Baby's"):
            self._validate(layout, str(link))

    def test_root_inside_git_directory_is_rejected(self, layout):
        git_dir = layout["project"] / ".git" / "objects"
        git_dir.mkdir(parents=True)
        with pytest.raises(ConfigurationError, match=r"\.git"):
            self._validate(layout, str(git_dir))

    def test_default_env_file_is_resolved_against_working_directory(self, layout, monkeypatch):
        monkeypatch.chdir(Path(layout["env"]).parent)
        with pytest.raises(ConfigurationError, match=r"\.env file"):
            validate_coding_repo_root(repo_root=str(Path(layout["env"]).parent), database_path="")

    def test_error_message_does_not_leak_env_contents(self, layout):
        with pytest.raises(ConfigurationError) as exc_info:
            self._validate(layout, str(Path(layout["env"]).parent))
        assert "not-a-real-secret" not in str(exc_info.value)


class TestWorkerStartupSandboxSafety:
    @pytest.fixture(autouse=True)
    def _isolate_registries(self):
        agent_registry.clear()
        permission_manager.clear()
        yield
        agent_registry.clear()
        permission_manager.clear()

    def test_default_bootstrap_refuses_missing_root_and_registers_nothing(self):
        with patch.object(settings, "coding_repo_root", None):
            with pytest.raises(ConfigurationError, match="not configured"):
                default_bootstrap()
        assert agent_registry.list_agents() == []
        assert permission_manager.get_agent_permissions("coding-agent") == []

    def test_default_bootstrap_refuses_root_containing_database(self, tmp_path: Path):
        with patch.object(settings, "coding_repo_root", str(tmp_path)):
            with patch("baby.tools.coding.path_resolver._default_database_path", return_value=str(tmp_path / "b.db")):
                with pytest.raises(ConfigurationError, match="database"):
                    default_bootstrap()
        assert agent_registry.list_agents() == []

    def test_default_bootstrap_with_valid_root_keeps_permission_boundaries(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        with patch.object(settings, "coding_repo_root", str(repo)):
            with patch("baby.tools.coding.path_resolver._default_database_path", return_value=str(tmp_path / "b.db")):
                default_bootstrap()
        perms = {p.category: p.level for p in permission_manager.get_agent_permissions("coding-agent")}
        assert perms == {
            PermissionCategory.LOCAL_READ: PermissionLevel.ALLOW,
            PermissionCategory.LOCAL_WRITE: PermissionLevel.APPROVAL_REQUIRED,
        }
        agent = agent_registry.get("coding-agent")
        assert {name: tool._repo_root for name, tool in agent.tools.items()} == {
            name: str(repo.resolve()) for name in ("list_files", "read_file", "write_file")
        }

    @pytest.mark.parametrize("unsafe", ["missing", "contains_db"])
    def test_supervisor_fails_before_spawning_workers(self, tmp_path: Path, unsafe):
        root = None if unsafe == "missing" else str(tmp_path)
        supervisor = WorkerSupervisor(worker_count=1, database_url=f"sqlite:///{tmp_path / 'baby.db'}")
        with patch.object(settings, "coding_repo_root", root):
            with pytest.raises(ConfigurationError):
                supervisor.start()
        assert supervisor.workers == []

    def test_custom_bootstrap_is_not_subject_to_coding_root_check(self, tmp_path: Path):
        supervisor = WorkerSupervisor(
            worker_count=0,
            database_url=f"sqlite:///{tmp_path / 'baby.db'}",
            bootstrap="tests.worker_helpers:bootstrap_scripted_agent",
        )
        with patch.object(settings, "coding_repo_root", None):
            with patch("baby.workers.supervisor.WorkerRecovery"):
                with patch("baby.persistence.database.db_manager.init_schema"):
                    supervisor.start()
        assert supervisor.workers == []

    def test_worker_cli_run_exits_with_config_error(self, capsys: pytest.CaptureFixture):
        from baby.workers.__main__ import main

        with patch.object(settings, "coding_repo_root", None):
            with patch("baby.logging.configure_logging"), patch("signal.signal"):
                assert main(["run", "--count", "1"]) == 2
        assert "Coding repository root is not configured" in capsys.readouterr().err
