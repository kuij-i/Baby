"""Repository-bounded list files tool."""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from baby.configuration import settings
from baby.core import PermissionCategory, PermissionLevel, ToolPermission, ToolSpec
from baby.logging import get_logger
from baby.tools.base import Tool, ToolResult
from baby.tools.coding.path_resolver import get_relative_repo_path, get_repo_root, resolve_safe_path

logger = get_logger(__name__)


class ListFilesTool(Tool):
    """Safely list files and directories within the repository root."""

    def __init__(self, repo_root: Optional[str] = None) -> None:
        self._repo_root = repo_root
        perm = ToolPermission(
            category=PermissionCategory.LOCAL_READ,
            level=PermissionLevel.ALLOW,
            description="List repository directory contents",
        )
        spec = ToolSpec(
            name="list_files",
            description="List files and directories within the configured repository root.",
            permissions_required=[perm],
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory path (defaults to root)"},
                    "recursive": {"type": "boolean", "description": "Whether to list recursively"},
                    "max_depth": {"type": "integer", "description": "Maximum directory recursion depth"},
                    "limit": {"type": "integer", "description": "Maximum number of entries to return"},
                },
            },
        )
        super().__init__(spec)

    async def execute(
        self,
        path: str = ".",
        recursive: bool = True,
        max_depth: Optional[int] = None,
        limit: Optional[int] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Execute the listing operation."""
        try:
            target_dir = resolve_safe_path(path or ".", repo_root=self._repo_root, allow_nonexistent=False)
            if not target_dir.is_dir():
                return ToolResult(
                    success=False,
                    error=f"Path is not a directory: '{path}'",
                )

            root = get_repo_root(self._repo_root)
            effective_depth = min(max_depth or 10, settings.coding_max_list_depth)
            effective_limit = min(limit or 100, settings.coding_max_list_entries)

            entries: List[Dict[str, Any]] = []
            truncated = False

            if not recursive:
                for item in sorted(target_dir.iterdir(), key=lambda p: p.name.lower()):
                    if item.name.lower() == ".git":
                        continue
                    try:
                        rel_path = item.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    is_dir = item.is_dir()
                    size = None if is_dir else item.stat().st_size
                    entries.append(
                        {
                            "path": rel_path,
                            "type": "directory" if is_dir else "file",
                            "size_bytes": size,
                        }
                    )
                    if len(entries) >= effective_limit:
                        truncated = True
                        break
            else:
                # Recursive traversal bounded by depth and limit
                for root_dir, dirs, files in os.walk(target_dir, followlinks=False):
                    # Exclude .git directories immediately from descent
                    dirs[:] = [d for d in sorted(dirs, key=lambda x: x.lower()) if d.lower() != ".git"]
                    current_path = Path(root_dir)

                    try:
                        depth = len(current_path.relative_to(target_dir).parts)
                    except ValueError:
                        continue

                    if depth > effective_depth:
                        dirs.clear()
                        continue

                    # Record subdirectories
                    for d in dirs:
                        dir_path = current_path / d
                        try:
                            rel_path = dir_path.relative_to(root).as_posix()
                        except ValueError:
                            continue
                        entries.append(
                            {
                                "path": rel_path,
                                "type": "directory",
                                "size_bytes": None,
                            }
                        )
                        if len(entries) >= effective_limit:
                            truncated = True
                            break

                    if truncated:
                        break

                    # Record files
                    for f in sorted(files, key=lambda x: x.lower()):
                        if f.lower() == ".git":
                            continue
                        file_path = current_path / f
                        try:
                            rel_path = file_path.relative_to(root).as_posix()
                            size = file_path.stat().st_size
                        except (ValueError, OSError):
                            continue
                        entries.append(
                            {
                                "path": rel_path,
                                "type": "file",
                                "size_bytes": size,
                            }
                        )
                        if len(entries) >= effective_limit:
                            truncated = True
                            break

                    if truncated:
                        break

            return ToolResult(
                success=True,
                output={
                    "path": get_relative_repo_path(target_dir, self._repo_root),
                    "entries": entries,
                    "total_count": len(entries),
                    "truncated": truncated,
                },
            )

        except Exception as exc:
            logger.warning("list_files tool execution failed", path=path, error=str(exc))
            return ToolResult(success=False, error=str(exc))
