"""Configuration management for BABY."""

import secrets
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    All API keys and secrets must come from environment variables.
    Never hard-code secrets.
    """

    # Logging
    log_level: str = "INFO"

    # Model Configuration
    default_model: str = "gpt-4"
    openai_api_key: Optional[str] = None
    openai_base_url: Optional[str] = None  # Override for NVIDIA/local endpoints
    anthropic_api_key: Optional[str] = None

    # Persistence & Database
    database_url: str = "sqlite:///./baby.db"
    persistence_backend: str = "sqlite"  # "sqlite" or "in_memory"
    memory_storage_type: str = "sqlite"  # "sqlite" or "in_memory"
    audit_storage_type: str = "sqlite"  # "sqlite" or "in_memory"

    # Audit
    audit_log_enabled: bool = True
    audit_log_level: str = "info"

    # Security
    enable_approval_workflow: bool = True

    # Coding Subsystem
    coding_repo_root: Optional[str] = None
    coding_max_file_size_bytes: int = 1_048_576  # 1 MB default limit
    coding_max_list_entries: int = 1000
    coding_max_list_depth: int = 20
    coding_max_iterations: int = 10
    coding_max_tool_calls_per_iteration: int = 5
    # Maximum retries for a single tool call (0 = no retries, bounded, max 10)
    coding_max_tool_retries: int = Field(default=3, ge=0, le=10)

    # Observability & API
    api_auth_token: Optional[str] = None
    api_admin_token: Optional[str] = None
    api_require_auth: bool = True
    # Explicit local development mode. Disabling authentication (api_require_auth=False) is
    # rejected unless this is also True, because every caller then gets admin access.
    development_mode: bool = False
    metrics_enabled: bool = True
    # Comma-separated list of engagement IDs the operator token is authorized for.
    # Controls server-side what an authenticated operator can access.
    # The client cannot expand this set via request headers or parameters.
    # Example: "engagement-A,engagement-B"
    # Default empty string → operator is scoped to the single "default" engagement.
    operator_engagements: str = ""

    # Durable workers (separate OS processes; see baby/workers)
    # worker_enabled=True makes the API lifespan supervise worker_count worker processes.
    # Workers can always be run standalone with `python -m baby.workers run`.
    worker_enabled: bool = False
    worker_count: int = 1  # 1..32
    worker_poll_interval_seconds: float = 1.0  # 0.01..60, idle wait between claim attempts
    worker_heartbeat_interval_seconds: float = 5.0  # 0.01..300, heartbeat + lease renewal cadence
    worker_stale_timeout_seconds: float = 30.0  # >= 2 x heartbeat; heartbeat older than this → STALE
    task_lease_duration_seconds: float = 60.0  # >= 2 x heartbeat; un-renewed lease expires after this
    worker_shutdown_grace_seconds: float = 10.0  # 0..600, wait for workers to stop before terminating
    worker_max_task_seconds: float = 3600.0  # 1..86400, bounded execution per claimed task

    class Config:
        env_file = ".env"
        case_sensitive = False


# Minimum length for configured API bearer tokens (secrets.token_hex(24) yields 48 characters).
MIN_API_TOKEN_LENGTH = 32

# Exact values shipped in earlier .env.example templates, plus markers of template text.
KNOWN_PLACEHOLDER_TOKENS = frozenset({"change-me-operator-token", "change-me-admin-token"})
PLACEHOLDER_TOKEN_MARKERS = ("change-me", "changeme", "replace-me", "replaceme", "placeholder", "your-token")

TOKEN_GENERATION_HINT = 'generate one with: python -c "import secrets; print(secrets.token_hex(24))"'


def api_token_problem(name: str, value: Optional[str]) -> Optional[str]:
    """Return why a configured API token is unacceptable, or None if it is acceptable.

    An unset or empty token is "not configured" and returns None. The returned message
    names the setting but never contains the token value.
    """
    if not value:
        return None
    if value != value.strip():
        return f"{name} must not have leading or trailing whitespace"
    lowered = value.lower()
    if lowered in KNOWN_PLACEHOLDER_TOKENS or any(marker in lowered for marker in PLACEHOLDER_TOKEN_MARKERS):
        return f"{name} is a placeholder value; set a unique secret ({TOKEN_GENERATION_HINT})"
    if len(value) < MIN_API_TOKEN_LENGTH:
        return f"{name} must be at least {MIN_API_TOKEN_LENGTH} characters ({TOKEN_GENERATION_HINT})"
    return None


def tokens_identical(first: Optional[str], second: Optional[str]) -> bool:
    """Constant-time check that two configured tokens are both set and equal."""
    if not first or not second:
        return False
    return secrets.compare_digest(first.encode("utf-8", "surrogatepass"), second.encode("utf-8", "surrogatepass"))


def validate_settings(cfg: "Settings") -> list:
    """Validate the given Settings instance and return a list of warning strings.

    Raises ConfigurationError for critical misconfigurations that must prevent startup.
    Returns a (possibly empty) list of non-fatal warning strings for soft issues.

    Never logs or returns secret values.
    """
    from baby.errors import ConfigurationError

    warnings: list = []

    # Critical: bounds must be positive
    if cfg.coding_max_iterations < 1:
        raise ConfigurationError(f"coding_max_iterations must be >= 1, got {cfg.coding_max_iterations}")
    if cfg.coding_max_tool_calls_per_iteration < 1:
        raise ConfigurationError(
            f"coding_max_tool_calls_per_iteration must be >= 1, got {cfg.coding_max_tool_calls_per_iteration}"
        )
    if cfg.coding_max_file_size_bytes < 1:
        raise ConfigurationError(f"coding_max_file_size_bytes must be >= 1, got {cfg.coding_max_file_size_bytes}")

    _validate_worker_settings(cfg)
    _validate_api_auth_settings(cfg)

    # Persistence backend validation (fail-closed if durable persistence is misconfigured)
    if cfg.persistence_backend not in ("sqlite", "in_memory"):
        raise ConfigurationError(
            f"Invalid persistence_backend '{cfg.persistence_backend}'. Expected 'sqlite' or 'in_memory'."
        )

    if cfg.persistence_backend == "sqlite":
        if not cfg.database_url or not cfg.database_url.strip():
            raise ConfigurationError(
                "persistence_backend='sqlite' requires a non-empty database_url. "
                "Do not fall back silently to in-memory mode in production."
            )

    # Non-critical: auth configuration warnings (critical auth checks run above)
    if cfg.api_require_auth and not cfg.api_auth_token and not cfg.api_admin_token:
        warnings.append(
            "api_require_auth=True but no api_auth_token or api_admin_token configured; "
            "all authenticated endpoints will return 401"
        )

    # Non-critical: development mode warning
    if not cfg.api_require_auth:
        warnings.append(
            "api_require_auth=False (development mode, development_mode=True): all callers get admin access; "
            "bind to 127.0.0.1 only and DO NOT use in production"
        )

    # Non-critical: unknown storage type warnings
    if cfg.memory_storage_type not in ("sqlite", "in_memory"):
        warnings.append(
            f"Unrecognized memory_storage_type '{cfg.memory_storage_type}'; expected 'sqlite' or 'in_memory'"
        )

    if cfg.audit_storage_type not in ("sqlite", "in_memory"):
        warnings.append(f"Unrecognized audit_storage_type '{cfg.audit_storage_type}'; expected 'sqlite' or 'in_memory'")

    return warnings


def _validate_api_auth_settings(cfg: "Settings") -> None:
    """Fail closed on API credential settings that would make authentication guessable or bypassable."""
    from baby.errors import ConfigurationError

    if not cfg.api_require_auth and not cfg.development_mode:
        raise ConfigurationError(
            "api_require_auth=False disables authentication and gives every caller admin access; it is only "
            "allowed together with development_mode=True (DEVELOPMENT_MODE=true) on a local machine. "
            "Set API_REQUIRE_AUTH=true and configure API_AUTH_TOKEN / API_ADMIN_TOKEN for any other deployment."
        )
    for name in ("api_auth_token", "api_admin_token"):
        problem = api_token_problem(name, getattr(cfg, name))
        if problem:
            raise ConfigurationError(problem)
    if cfg.api_require_auth and tokens_identical(cfg.api_auth_token, cfg.api_admin_token):
        raise ConfigurationError(
            "api_auth_token and api_admin_token must be different: an identical value would give operators "
            "admin access. Generate a separate secret for each."
        )


def _validate_worker_settings(cfg: "Settings") -> None:
    """Fail closed on worker timing settings that would make leases or stale detection unsafe."""
    from baby.errors import ConfigurationError

    def _bounded(name: str, low: float, high: float) -> float:
        value = getattr(cfg, name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not (low <= value <= high):
            raise ConfigurationError(f"{name} must be between {low} and {high}, got {value!r}")
        return float(value)

    _bounded("worker_count", 1, 32)
    if not isinstance(cfg.worker_count, int):
        raise ConfigurationError(f"worker_count must be an integer, got {cfg.worker_count!r}")
    _bounded("worker_poll_interval_seconds", 0.01, 60)
    heartbeat = _bounded("worker_heartbeat_interval_seconds", 0.01, 300)
    stale = _bounded("worker_stale_timeout_seconds", 0.02, 3600)
    lease = _bounded("task_lease_duration_seconds", 0.02, 3600)
    _bounded("worker_shutdown_grace_seconds", 0, 600)
    _bounded("worker_max_task_seconds", 1, 86400)

    # A worker renews its heartbeat and lease once per heartbeat interval; requiring two
    # missed intervals before a worker/lease is considered lost prevents false recovery.
    if stale < 2 * heartbeat:
        raise ConfigurationError(
            "worker_stale_timeout_seconds must be at least 2 x worker_heartbeat_interval_seconds "
            f"({stale} < 2 x {heartbeat})"
        )
    if lease < 2 * heartbeat:
        raise ConfigurationError(
            "task_lease_duration_seconds must be at least 2 x worker_heartbeat_interval_seconds "
            f"({lease} < 2 x {heartbeat})"
        )
    if cfg.worker_enabled and cfg.persistence_backend != "sqlite":
        raise ConfigurationError(
            "worker_enabled=True requires persistence_backend='sqlite' (workers share state via SQLite)"
        )


# Global settings instance
settings = Settings()
