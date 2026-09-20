"""Provider types shared across model provider implementations."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """A single message in a chat conversation."""

    role: str = Field(..., min_length=1)
    content: str = Field(default="")
    name: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None


class TokenUsage(BaseModel):
    """Token usage information from a provider response."""

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class ProviderResponse(BaseModel):
    """Normalized response from any model provider."""

    content: str = Field(default="")
    model: str = Field(default="")
    usage: TokenUsage = Field(default_factory=TokenUsage)
    finish_reason: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    raw_response: Optional[Dict[str, Any]] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ToolDefinition(BaseModel):
    """A tool definition for function-calling capable models."""

    name: str = Field(..., min_length=1)
    description: str = Field(default="")
    parameters: Dict[str, Any] = Field(default_factory=dict)
