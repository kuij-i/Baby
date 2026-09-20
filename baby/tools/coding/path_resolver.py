"""Repository path resolution and boundary enforcement.

All coding tools MUST resolve filesystem paths through this module.
Defends against:
- Path traversal (../, relative escapes)
- Absolute path escapes outside the repository root
- Symlink escapes
- Access to .git internals
- Unconfigured repository root (fails closed)
"""

from pathlib import Path
from typing import Optional

from baby.configuration import settings
from baby.errors import ConfigurationError, PathTraversalError, RepositoryBoundaryError, ValidationError
from baby.logging import get_logger

logger = get_logger(__name__)


def get_repo_root(repo_root: Optional[str] = None) -> Path:
    """Resolve and validate the configured coding repository root.

    Args:
        repo_root: Optional explicit root path. If not provided, falls back
            to ``settings.coding_repo_root``.

    Returns:
        Canonical, resolved Path to the repository root directory.

    Raises:
        ConfigurationError: If the repository root is not configured or does not exist.
    """
    configured = repo_root or settings.coding_repo_root
    if not configured or not str(configured).strip():
        raise ConfigurationError(
            "Coding repository root is not configured. Set CODING_REPO_ROOT in environment or configuration."
        )

    root_path = Path(configured).expanduser().resolve()
    if not root_path.exists():
        raise ConfigurationError(f"Configured repository root does not exist: {root_path}")
    if not root_path.is_dir():
        raise ConfigurationError(f"Configured repository root is not a directory: {root_path}")

    return root_path


def resolve_safe_path(
    path: str,
    repo_root: Optional[str] = None,
    allow_nonexistent: bool = False,
) -> Path:
    """Resolve and strictly validate a path within the repository root.

    Args:
        path: Path string (relative to repository root, or absolute within root).
        repo_root: Optional explicit repository root.
        allow_nonexistent: If True, the target file does not need to exist yet
            (used for write operations). Its parent must still reside within root.

    Returns:
        Canonical, resolved Path strictly contained within the repository root.

    Raises:
        ValidationError: If path argument is invalid or empty.
        ConfigurationError: If repository root is unconfigured or invalid.
        PathTraversalError: If path attempts to escape the repository root.
        RepositoryBoundaryError: If path attempts to access .git directory or files.
        FileNotFoundError: If allow_nonexistent is False and target does not exist.
    """
    if not path or not str(path).strip():
        raise ValidationError("Path argument must not be empty")

    if "\x00" in path:
        raise ValidationError("Path contains invalid null byte characters")

    root = get_repo_root(repo_root)

    # Convert to Path object
    raw_path = Path(path.strip())

    # Resolve target path
    if raw_path.is_absolute():
        target = raw_path.resolve()
    else:
        target = (root / raw_path).resolve()

    # 1. Traversal / boundary check: must be strictly inside root
    try:
        rel = target.relative_to(root)
    except ValueError:
        logger.warning("Path traversal attempt blocked", path=path, root=str(root))
        raise PathTraversalError(f"Path '{path}' traverses outside the configured repository root")

    # If non-existent is allowed, also ensure existing parent resolves within root
    if allow_nonexistent and not target.exists():
        try:
            target.parent.resolve().relative_to(root)
        except ValueError:
            logger.warning("Parent path traversal attempt blocked", path=path, root=str(root))
            raise PathTraversalError(f"Parent directory of '{path}' traverses outside the repository root")

    # 2. .git protection check: no component of the relative path may be .git
    for part in rel.parts:
        part_lower = part.lower()
        if part_lower == ".git" or part_lower.startswith(".git"):
            logger.warning("Attempted access to .git blocked", path=path)
            raise RepositoryBoundaryError(f"Access to .git directory or files is strictly forbidden: '{path}'")

    # 3. Existence check if not creating
    if not allow_nonexistent and not target.exists():
        raise FileNotFoundError(f"File not found within repository: '{path}'")

    return target


def get_relative_repo_path(target: Path, repo_root: Optional[str] = None) -> str:
    """Return the normalized, forward-slash relative path from the repository root."""
    root = get_repo_root(repo_root)
    return target.resolve().relative_to(root).as_posix()
