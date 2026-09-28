"""Configuration management for BABY."""

from typing import Optional

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


# Global settings instance
settings = Settings()
