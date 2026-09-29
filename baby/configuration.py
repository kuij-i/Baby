"""Configuration management for BABY."""

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

    # Database
    database_url: str = "sqlite:///./baby.db"
    memory_storage_type: str = "sqlite"

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
    metrics_enabled: bool = True
    # Comma-separated list of engagement IDs the operator token is authorized for.
    # Controls server-side what an authenticated operator can access.
    # The client cannot expand this set via request headers or parameters.
    # Example: "engagement-A,engagement-B"
    # Default empty string → operator is scoped to the single "default" engagement.
    operator_engagements: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = False


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
            f"coding_max_tool_calls_per_iteration must be >= 1, " f"got {cfg.coding_max_tool_calls_per_iteration}"
        )
    if cfg.coding_max_file_size_bytes < 1:
        raise ConfigurationError(f"coding_max_file_size_bytes must be >= 1, got {cfg.coding_max_file_size_bytes}")

    # Non-critical: auth configuration warnings
    if cfg.api_require_auth and not cfg.api_auth_token and not cfg.api_admin_token:
        warnings.append(
            "api_require_auth=True but no api_auth_token or api_admin_token configured; "
            "all authenticated endpoints will return 401"
        )

    # Non-critical: development mode warning
    if not cfg.api_require_auth:
        warnings.append(
            "api_require_auth=False (development mode): all callers get admin access; " "DO NOT use in production"
        )

    # Non-critical: unknown memory storage type
    if cfg.memory_storage_type not in ("sqlite", "in_memory"):
        warnings.append(
            f"Unrecognized memory_storage_type '{cfg.memory_storage_type}'; " "expected 'sqlite' or 'in_memory'"
        )

    return warnings


# Global settings instance
settings = Settings()
