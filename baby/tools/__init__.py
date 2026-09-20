"""Tool framework for BABY.

Provides tool abstraction, registry, and execution with permission checks.
"""

from baby.tools.base import Tool, ToolResult
from baby.tools.coding import ListFilesTool, ReadFileTool, WriteFileTool
from baby.tools.executor import ToolExecutor, tool_executor
from baby.tools.registry import ToolRegistry, tool_registry

__all__ = [
    "Tool",
    "ToolResult",
    "tool_registry",
    "ToolRegistry",
    "tool_executor",
    "ToolExecutor",
    "ListFilesTool",
    "ReadFileTool",
    "WriteFileTool",
]
