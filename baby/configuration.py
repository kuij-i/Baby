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
    anthropic_api_key: Optional[str] = None

    # Database
    database_url: str = "sqlite:///./baby.db"
    memory_storage_type: str = "sqlite"

    # Audit
    audit_log_enabled: bool = True
    audit_log_level: str = "info"

    # Security
    enable_approval_workflow: bool = True

    class Config:
        env_file = ".env"
        case_sensitive = False


# Global settings instance
settings = Settings()
