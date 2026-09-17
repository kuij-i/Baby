"""Tool registry for managing all tools in the system."""

from typing import Dict, List

from baby.errors import ToolNotFoundError
from baby.tools.base import Tool


class ToolRegistry:
    """Registry for all tools in BABY.

    Provides central management and lookup of tools by name.
    """

    def __init__(self) -> None:
        """Initialize empty tool registry."""
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool.

        Args:
            tool: Tool instance to register

        Raises:
            ValueError: If tool is already registered
        """
        tool_name = tool.spec.name
        if tool_name in self._tools:
            raise ValueError(f"Tool {tool_name} is already registered")

        self._tools[tool_name] = tool

    def unregister(self, tool_name: str) -> None:
        """Unregister a tool.

        Args:
            tool_name: Name of tool to unregister

        Raises:
            ToolNotFoundError: If tool is not registered
        """
        if tool_name not in self._tools:
            raise ToolNotFoundError(f"Tool {tool_name} not registered")

        del self._tools[tool_name]

    def get(self, tool_name: str) -> Tool:
        """Get a tool by name.

        Args:
            tool_name: Name of tool to retrieve

        Returns:
            Tool instance

        Raises:
            ToolNotFoundError: If tool is not registered
        """
        if tool_name not in self._tools:
            raise ToolNotFoundError(f"Tool {tool_name} not registered")

        return self._tools[tool_name]

    def list_tools(self) -> List[Tool]:
        """List all registered tools.

        Returns:
            List of all tools
        """
        return list(self._tools.values())

    def get_tools_by_permission(self, permission_category: str) -> List[Tool]:
        """Get all tools requiring a specific permission.

        Args:
            permission_category: Permission category to filter by

        Returns:
            List of tools requiring the permission
        """
        return [
            tool
            for tool in self._tools.values()
            if any(perm.category.value == permission_category for perm in tool.permissions_required)
        ]

    def clear(self) -> None:
        """Clear all tools (for testing only)."""
        self._tools.clear()


# Global tool registry instance
tool_registry = ToolRegistry()
