"""Tool framework for BABY.

Provides tool abstraction, registry, and execution with permission checks.
"""

from baby.tools.base import Tool, ToolResult
from baby.tools.coding import (
    SAFE_CODING_TOOL_NAMES,
    ListRepositoryFilesTool,
    ReadRepositoryFileTool,
    RepositoryTool,
    WriteRepositoryFileTool,
    build_safe_coding_tools,
    register_safe_coding_tools,
)
from baby.tools.executor import ToolExecutor, tool_executor
from baby.tools.registry import ToolRegistry, tool_registry

__all__ = [
    "Tool",
    "ToolResult",
    "SAFE_CODING_TOOL_NAMES",
    "RepositoryTool",
    "ListRepositoryFilesTool",
    "ReadRepositoryFileTool",
    "WriteRepositoryFileTool",
    "build_safe_coding_tools",
    "register_safe_coding_tools",
    "tool_registry",
    "ToolRegistry",
    "tool_executor",
    "ToolExecutor",
]
