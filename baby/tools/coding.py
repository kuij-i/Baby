"""Repository-bounded tools for the coding agent."""

from pathlib import Path
from typing import Any

from baby.core import PermissionCategory, PermissionLevel, ToolPermission, ToolSpec
from baby.errors import PermissionDeniedError
from baby.tools.base import Tool, ToolResult
from baby.tools.registry import ToolRegistry, tool_registry

SAFE_CODING_TOOL_NAMES = {
    "repository_list_files",
    "repository_read_file",
    "repository_write_file",
}


class RepositoryTool(Tool):
    """Base class for tools constrained to a repository root."""

    def __init__(self, spec: ToolSpec, repo_root: str | Path) -> None:
        super().__init__(spec)
        self.repo_root = Path(repo_root).resolve()

    def _resolve_path(self, path: str) -> Path:
        candidate = Path(path)
        resolved = (candidate if candidate.is_absolute() else self.repo_root / candidate).resolve()
        try:
            resolved.relative_to(self.repo_root)
        except ValueError as exc:
            raise PermissionDeniedError(f"Path {path} is outside the repository root") from exc

        if ".git" in resolved.parts:
            raise PermissionDeniedError("Access to .git is not allowed")
        return resolved

    def _relative_path(self, path: Path) -> str:
        return path.relative_to(self.repo_root).as_posix()


class ListRepositoryFilesTool(RepositoryTool):
    """List repository files from an allow-listed root."""

    async def execute(self, **kwargs: Any) -> ToolResult:
        directory = self._resolve_path(kwargs.get("path", "."))
        if not directory.exists() or not directory.is_dir():
            return ToolResult(success=False, error=f"Directory not found: {kwargs.get('path', '.')}")

        files = sorted(self._relative_path(path) for path in directory.rglob("*") if path.is_file())
        return ToolResult(
            success=True,
            output={"path": self._relative_path(directory), "files": files},
        )


class ReadRepositoryFileTool(RepositoryTool):
    """Read a text file within the repository root."""

    async def execute(self, **kwargs: Any) -> ToolResult:
        target = self._resolve_path(kwargs["path"])
        if not target.exists() or not target.is_file():
            return ToolResult(success=False, error=f"File not found: {kwargs['path']}")

        return ToolResult(
            success=True,
            output={"path": self._relative_path(target), "content": target.read_text(encoding="utf-8")},
        )


class WriteRepositoryFileTool(RepositoryTool):
    """Write a text file within the repository root."""

    async def execute(self, **kwargs: Any) -> ToolResult:
        target = self._resolve_path(kwargs["path"])
        if target.exists() and target.is_dir():
            return ToolResult(success=False, error=f"Cannot write directory: {kwargs['path']}")

        target.parent.mkdir(parents=True, exist_ok=True)
        content = kwargs["content"]
        target.write_text(content, encoding="utf-8")
        return ToolResult(
            success=True,
            output={
                "path": self._relative_path(target),
                "bytes_written": len(content.encode("utf-8")),
            },
        )


def build_safe_coding_tools(repo_root: str | Path) -> list[Tool]:
    """Construct the built-in safe coding tools for a repository."""
    read_permission = ToolPermission(
        category=PermissionCategory.LOCAL_READ,
        level=PermissionLevel.ALLOW,
        description="Read files inside the repository root",
    )
    write_permission = ToolPermission(
        category=PermissionCategory.LOCAL_WRITE,
        level=PermissionLevel.APPROVAL_REQUIRED,
        description="Write files inside the repository root with approval",
    )
    return [
        ListRepositoryFilesTool(
            ToolSpec(
                name="repository_list_files",
                description="List files inside the repository root",
                permissions_required=[read_permission],
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
            ),
            repo_root,
        ),
        ReadRepositoryFileTool(
            ToolSpec(
                name="repository_read_file",
                description="Read a UTF-8 text file inside the repository root",
                permissions_required=[read_permission],
                input_schema={"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
            ),
            repo_root,
        ),
        WriteRepositoryFileTool(
            ToolSpec(
                name="repository_write_file",
                description="Write a UTF-8 text file inside the repository root",
                permissions_required=[write_permission],
                input_schema={
                    "type": "object",
                    "required": ["path", "content"],
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                },
            ),
            repo_root,
        ),
    ]


def register_safe_coding_tools(
    repo_root: str | Path,
    registry: ToolRegistry | None = None,
) -> list[Tool]:
    """Register built-in safe coding tools and return them."""
    active_registry = registry or tool_registry
    registered: list[Tool] = []
    for tool in build_safe_coding_tools(repo_root):
        if any(existing.name == tool.name for existing in active_registry.list_tools()):
            registered.append(active_registry.get(tool.name))
            continue
        active_registry.register(tool)
        registered.append(tool)
    return registered
