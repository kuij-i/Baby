"""Tool framework for BABY.

Provides tool abstraction, registry, and execution with permission checks.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional

from baby.core import ToolSpec
from baby.logging import get_logger

logger = get_logger(__name__)


class ToolResult:
    """Result of tool execution."""

    def __init__(
        self,
        success: bool,
        output: Any = None,
        error: Optional[str] = None,
        execution_time_ms: int = 0,
    ) -> None:
        """Initialize tool result.

        Args:
            success: Whether execution succeeded
            output: Tool output
            error: Error message if failed
            execution_time_ms: Execution time in milliseconds
        """
        self.success = success
        self.output = output
        self.error = error
        self.execution_time_ms = execution_time_ms
        self.timestamp = datetime.utcnow()

    def __repr__(self) -> str:
        return f"ToolResult(success={self.success}, " f"execution_time_ms={self.execution_time_ms})"


class Tool(ABC):
    """Base class for all tools in BABY.

    Tools provide abstracted access to system capabilities with permission checks.
    """

    def __init__(self, spec: ToolSpec) -> None:
        """Initialize tool with specification.

        Args:
            spec: Tool specification with name, permissions, schema
        """
        self.spec = spec
        self._retry_count = 0

    @property
    def name(self) -> str:
        """Get tool name."""
        return self.spec.name

    @property
    def permissions_required(self):
        """Get permissions required for this tool."""
        return self.spec.permissions_required

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the tool.

        Args:
            **kwargs: Tool-specific arguments

        Returns:
            ToolResult with success status and output
        """
        pass

    async def execute_with_retry(self, **kwargs: Any) -> ToolResult:
        """Execute tool with retry on failure.

        Args:
            **kwargs: Tool-specific arguments

        Returns:
            ToolResult after retries
        """
        attempt = 0
        last_error = None

        while attempt <= self.spec.retry_count:
            try:
                result = await self.execute(**kwargs)
                if result.success:
                    return result
                last_error = result.error
                attempt += 1
            except Exception as e:
                last_error = str(e)
                attempt += 1

        return ToolResult(
            success=False,
            error=f"Tool failed after {self.spec.retry_count + 1} attempts: {last_error}",
        )

    def validate_input(self, **kwargs: Any) -> None:
        """Validate input against tool schema.

        Args:
            **kwargs: Tool input

        Raises:
            ValueError: If input doesn't match schema
        """
        # Basic implementation - can be extended with JSON schema validation
        if not self.spec.input_schema:
            return

        required = self.spec.input_schema.get("required", [])
        for field in required:
            if field not in kwargs:
                raise ValueError(f"Required input field missing: {field}")
