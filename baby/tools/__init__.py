"""Tool framework for BABY.

Provides tool abstraction, registry, and execution with permission checks.
"""

from baby.tools.base import Tool, ToolResult
from baby.tools.registry import tool_registry, ToolRegistry
from baby.tools.executor import tool_executor, ToolExecutor

__all__ = [
    "Tool",
    "ToolResult",
    "tool_registry",
    "ToolRegistry",
    "tool_executor",
    "ToolExecutor",
]
