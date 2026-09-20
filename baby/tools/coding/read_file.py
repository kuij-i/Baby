"""Repository-bounded read file tool."""

from typing import Any, Optional

from baby.configuration import settings
from baby.core import PermissionCategory, PermissionLevel, ToolPermission, ToolSpec
from baby.logging import get_logger
from baby.tools.base import Tool, ToolResult
from baby.tools.coding.path_resolver import get_relative_repo_path, resolve_safe_path

logger = get_logger(__name__)


class ReadFileTool(Tool):
    """Safely read text files within the repository root."""

    def __init__(self, repo_root: Optional[str] = None) -> None:
        self._repo_root = repo_root
        perm = ToolPermission(
            category=PermissionCategory.LOCAL_READ,
            level=PermissionLevel.ALLOW,
            description="Read file contents within repository",
        )
        spec = ToolSpec(
            name="read_file",
            description="Read a text file within the configured repository root.",
            permissions_required=[perm],
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path within repository"},
                    "max_bytes": {"type": "integer", "description": "Maximum bytes to read"},
                },
                "required": ["path"],
            },
        )
        super().__init__(spec)

    async def execute(
        self,
        path: str = "",
        max_bytes: Optional[int] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Execute the read operation."""
        if not path:
            return ToolResult(success=False, error="Required parameter 'path' is missing")
        try:
            target = resolve_safe_path(path, repo_root=self._repo_root, allow_nonexistent=False)

            if target.is_dir():
                return ToolResult(
                    success=False,
                    error=f"Cannot read a directory as a file: '{path}'",
                )

            effective_max_bytes = max_bytes or settings.coding_max_file_size_bytes
            stat_result = target.stat()
            if stat_result.st_size > effective_max_bytes:
                return ToolResult(
                    success=False,
                    error=(
                        f"File size ({stat_result.st_size} bytes) exceeds maximum "
                        f"allowed limit ({effective_max_bytes} bytes): '{path}'"
                    ),
                )

            try:
                content = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return ToolResult(
                    success=False,
                    error=f"File '{path}' is not valid UTF-8 text",
                )

            return ToolResult(
                success=True,
                output={
                    "path": get_relative_repo_path(target, self._repo_root),
                    "content": content,
                    "size_bytes": stat_result.st_size,
                },
            )

        except Exception as exc:
            logger.warning("read_file tool execution failed", path=path, error=str(exc))
            return ToolResult(success=False, error=str(exc))
