"""Sensitive data redaction for observability outputs.

Ensures credentials, API keys, passwords, and private tokens are never
leaked through observability endpoints or logs.
"""

import re
from typing import Any, Dict, Set

from pydantic import BaseModel

SENSITIVE_KEY_PATTERNS: Set[str] = {
    "api_key",
    "apikey",
    "access_token",
    "auth",
    "authorization",
    "bearer",
    "credential",
    "credentials",
    "jwt",
    "openai_api_key",
    "anthropic_api_key",
    "password",
    "passwd",
    "private_key",
    "secret",
    "secrets",
    "token",
    "webhook_url",
    "cookie",
    "session_id",
}

# Regex to detect API keys or bearer tokens in arbitrary strings
SECRET_STRING_PATTERNS = [
    re.compile(r"(sk-[a-zA-Z0-9_\-]{20,})", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[a-zA-Z0-9_\-\.]{15,}", re.IGNORECASE),
    re.compile(r"(ghp_[a-zA-Z0-9]{30,})", re.IGNORECASE),
    re.compile(r"([a-f0-9]{32,64})", re.IGNORECASE),  # Hex secrets (e.g. SHA/MD5 tokens)
]

REDACTED_VALUE = "[REDACTED]"


def _is_sensitive_key(key: str) -> bool:
    """Check if a dictionary key indicates sensitive data."""
    normalized = key.lower().replace("-", "_").strip()
    if normalized in SENSITIVE_KEY_PATTERNS:
        return True
    return any(pattern in normalized for pattern in SENSITIVE_KEY_PATTERNS)


def _sanitize_string(val: str) -> str:
    """Scrub known secret formats from a string."""
    sanitized = val
    # Bearer token regex
    sanitized = re.sub(r"(Bearer\s+)[a-zA-Z0-9_\-\.]{15,}", r"\1[REDACTED]", sanitized, flags=re.IGNORECASE)
    # OpenAI key regex
    sanitized = re.sub(r"sk-[a-zA-Z0-9_\-]{20,}", REDACTED_VALUE, sanitized, flags=re.IGNORECASE)
    # GitHub personal token regex
    sanitized = re.sub(r"ghp_[a-zA-Z0-9]{30,}", REDACTED_VALUE, sanitized, flags=re.IGNORECASE)
    return sanitized


def redact_sensitive_data(data: Any) -> Any:
    """Recursively scrub sensitive keys and token strings from data structures.

    Returns a deep copy with sensitive values replaced by '[REDACTED]'.
    The original data structure is not mutated.
    """
    if data is None:
        return None

    if isinstance(data, BaseModel):
        data_dict = data.model_dump()
        return redact_sensitive_data(data_dict)

    if isinstance(data, dict):
        result: Dict[str, Any] = {}
        for k, v in data.items():
            str_key = str(k)
            if _is_sensitive_key(str_key):
                result[str_key] = REDACTED_VALUE
            else:
                result[str_key] = redact_sensitive_data(v)
        return result

    if isinstance(data, list):
        return [redact_sensitive_data(item) for item in data]

    if isinstance(data, tuple):
        return tuple(redact_sensitive_data(item) for item in data)

    if isinstance(data, set):
        return {redact_sensitive_data(item) for item in data}

    if isinstance(data, str):
        return _sanitize_string(data)

    return data
