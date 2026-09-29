"""Abstract filesystem boundary for coding operations.

Establishes an explicit architectural interface between CodingAgent/tools and
the underlying filesystem implementation.

Conceptually:
    CodingAgent / Tools
           │
           ▼
    CodingFilesystem interface
           ├── LocalCodingFilesystem (current authoritative Python implementation)
           └── Future Rust implementation (e.g. native sandboxed crate via PyO3)

This boundary guarantees that future native optimizations or OS-level sandbox
mechanisms can be introduced without modifying CodingAgent contracts, tool specs,
or security permission enforcement.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

from baby.configuration import settings
from baby.errors import RepositoryBoundaryError
from baby.tools.coding.path_resolver import (
    get_relative_repo_path,
    get_repo_root,
    resolve_safe_path,
)


class CodingFilesystem(ABC):
    """Abstract interface defining required repository filesystem operations."""

    @abstractmethod
    def resolve_path(self, relative_path: str, allow_nonexistent: bool = False) -> Path:
        """Resolve a relative path safely within the configured repository root.

        Raises:
            RepositoryBoundaryError: If repo root is not configured or path targets .git
            PathTraversalError: If path attempts to escape repository root
        """
        raise NotImplementedError

    @abstractmethod
    def list_files(
        self,
        directory: str = "",
        max_depth: Optional[int] = None,
        max_entries: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """List files and directories bounded by depth, entry limits, and dotfile filters."""
        raise NotImplementedError

    @abstractmethod
    def read_file(self, relative_path: str, max_bytes: Optional[int] = None) -> str:
        """Read and decode UTF-8 text from a file within the repository."""
        raise NotImplementedError

    @abstractmethod
    def write_file(self, relative_path: str, content: str) -> int:
        """Write UTF-8 text to a file within the repository, creating parent dirs if needed."""
        raise NotImplementedError

    @abstractmethod
    def get_root(self) -> Path:
        """Return the resolved absolute Path to the repository root."""
        raise NotImplementedError


class LocalCodingFilesystem(CodingFilesystem):
    """Authoritative Python implementation of CodingFilesystem.

    Enforces:
    - Repository root boundary containment
    - Strict .git isolation
    - Max file size limits
    - Max directory traversal depth & entry count limits
    """

    def resolve_path(self, relative_path: str, allow_nonexistent: bool = False) -> Path:
        return resolve_safe_path(relative_path, allow_nonexistent=allow_nonexistent)

    def list_files(
        self,
        directory: str = "",
        max_depth: Optional[int] = None,
        max_entries: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        depth = max_depth if max_depth is not None else settings.coding_max_list_depth
        limit = max_entries if max_entries is not None else settings.coding_max_list_entries

        root = get_repo_root()
        target_dir = resolve_safe_path(directory) if directory else root

        if not target_dir.is_dir():
            raise RepositoryBoundaryError(f"Target path is not a directory: '{directory}'")

        entries: List[Dict[str, Any]] = []

        def _walk(current: Path, current_depth: int) -> None:
            if len(entries) >= limit or current_depth > depth:
                return

            try:
                children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except PermissionError:
                return

            for child in children:
                if len(entries) >= limit:
                    break

                # Protect .git directory and internal files
                if child.name == ".git":
                    continue

                rel = get_relative_repo_path(child)
                is_dir = child.is_dir()
                size = child.stat().st_size if not is_dir else 0

                entries.append(
                    {
                        "path": rel,
                        "type": "directory" if is_dir else "file",
                        "size_bytes": size,
                    }
                )

                if is_dir and current_depth < depth:
                    _walk(child, current_depth + 1)

        _walk(target_dir, 0)
        return entries

    def read_file(self, relative_path: str, max_bytes: Optional[int] = None) -> str:
        limit = max_bytes if max_bytes is not None else settings.coding_max_file_size_bytes
        safe_path = self.resolve_path(relative_path)

        if not safe_path.exists():
            raise FileNotFoundError(f"File not found: '{relative_path}'")
        if safe_path.is_dir():
            raise IsADirectoryError(f"Target is a directory: '{relative_path}'")

        file_size = safe_path.stat().st_size
        if file_size > limit:
            raise ValueError(f"File size ({file_size} bytes) exceeds configured limit ({limit} bytes)")

        try:
            return safe_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"File is not valid UTF-8 text: '{relative_path}'") from exc

    def write_file(self, relative_path: str, content: str) -> int:
        safe_path = self.resolve_path(relative_path, allow_nonexistent=True)

        # Enforce file size limit on payload
        content_bytes = content.encode("utf-8")
        if len(content_bytes) > settings.coding_max_file_size_bytes:
            raise ValueError(
                f"Content size ({len(content_bytes)} bytes) exceeds limit ({settings.coding_max_file_size_bytes} bytes)"
            )

        # Ensure parent directory exists
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path.write_text(content, encoding="utf-8")
        return len(content_bytes)

    def get_root(self) -> Path:
        return get_repo_root()


def get_coding_filesystem() -> CodingFilesystem:
    """Return the active CodingFilesystem implementation.

    Defaults to LocalCodingFilesystem. In future phases, can be configured to
    return a native Rust-backed implementation without breaking callers.
    """
    return LocalCodingFilesystem()
