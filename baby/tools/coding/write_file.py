"""Repository-bounded write file tool."""

from typing import Any, Optional

from baby.configuration import settings
from baby.core import PermissionCategory, PermissionLevel, ToolPermission, ToolSpec
from baby.logging import get_logger
from baby.tools.base import Tool, ToolResult
from baby.tools.coding.path_resolver import get_relative_repo_path, resolve_safe_path

logger = get_logger(__name__)


class WriteFileTool(Tool):
    """Safely create or overwrite text files within the repository root.

    Requires LOCAL_WRITE permission, which defaults to APPROVAL_REQUIRED.
    Does NOT execute the written file or run any subprocess commands.
    """

    def __init__(self, repo_root: Optional[str] = None) -> None:
        self._repo_root = repo_root
        perm = ToolPermission(
            category=PermissionCategory.LOCAL_WRITE,
            level=PermissionLevel.APPROVAL_REQUIRED,
            description="Write or modify files within repository (requires approval)",
        )
        spec = ToolSpec(
            name="write_file",
            description="Create or modify a text file within the configured repository root.",
            permissions_required=[perm],
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path within repository"},
                    "content": {"type": "string", "description": "Text content to write to the file"},
                    "create_parents": {"type": "boolean", "description": "Create missing parent directories"},
                },
                "required": ["path", "content"],
            },
        )
        super().__init__(spec)

    async def execute(
        self,
        path: str = "",
        content: str = "",
        create_parents: bool = True,
        **kwargs: Any,
    ) -> ToolResult:
        """Execute the write operation."""
        if not path:
            return ToolResult(success=False, error="Required parameter 'path' is missing")
        if content is None:
            return ToolResult(success=False, error="Required parameter 'content' is missing")
        try:
            target = resolve_safe_path(path, repo_root=self._repo_root, allow_nonexistent=True)

            if target.exists() and target.is_dir():
                return ToolResult(
                    success=False,
                    error=f"Cannot overwrite directory with a file: '{path}'",
                )

            # Check size before writing
            encoded_bytes = content.encode("utf-8")
            if len(encoded_bytes) > settings.coding_max_file_size_bytes:
                return ToolResult(
                    success=False,
                    error=(
                        f"Content size ({len(encoded_bytes)} bytes) exceeds maximum "
                        f"allowed file size ({settings.coding_max_file_size_bytes} bytes)"
                    ),
                )

            is_new_file = not target.exists()

            if create_parents:
                target.parent.mkdir(parents=True, exist_ok=True)

            target.write_text(content, encoding="utf-8")

            rel_path = get_relative_repo_path(target, self._repo_root)
            logger.info(
                "File written successfully within repository",
                path=rel_path,
                bytes_written=len(encoded_bytes),
                created=is_new_file,
            )

            return ToolResult(
                success=True,
                output={
                    "path": rel_path,
                    "bytes_written": len(encoded_bytes),
                    "created": is_new_file,
                },
            )

        except Exception as exc:
            logger.warning("write_file tool execution failed", path=path, error=str(exc))
            return ToolResult(success=False, error=str(exc))
