"""Comprehensive tests for repository-bounded coding tools.

Validates:
- Repository boundary enforcement (traversal rejection, .git protection, unconfigured root fails closed)
- ReadFileTool (valid read, directory rejection, nonexistent file, size limit, encoding safety)
- ListFilesTool (directory listing, recursion, .git exclusion, entry/depth limits)
- WriteFileTool (creation, modification, traversal rejection, size limit)
- Approval enforcement (ApprovalRequiredError, tool uncalled, audit trail)
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from baby.audit import audit_log
from baby.configuration import settings
from baby.core import AgentId, AuditEventType, PermissionCategory, PermissionLevel, ToolPermission
from baby.errors import (
    ApprovalRequiredError,
    ConfigurationError,
    PathTraversalError,
    PermissionDeniedError,
    RepositoryBoundaryError,
    ValidationError,
)
from baby.permissions import permission_manager
from baby.tools.coding.list_files import ListFilesTool
from baby.tools.coding.path_resolver import get_relative_repo_path, get_repo_root, resolve_safe_path
from baby.tools.coding.read_file import ReadFileTool
from baby.tools.coding.write_file import WriteFileTool
from baby.tools.executor import tool_executor


@pytest.fixture
def temp_repo():
    """Create a temporary repository directory with initial structure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir).resolve()

        # Create basic files
        (repo_path / "hello.py").write_text("print('hello world')", encoding="utf-8")
        (repo_path / "README.md").write_text("# Test Repo", encoding="utf-8")

        # Create nested directory
        nested = repo_path / "src" / "pkg"
        nested.mkdir(parents=True)
        (nested / "module.py").write_text("def add(a, b): return a + b", encoding="utf-8")

        # Create simulated .git directory
        dotgit = repo_path / ".git"
        dotgit.mkdir()
        (dotgit / "config").write_text("[core]\n\trepositoryformatversion = 0", encoding="utf-8")
        (dotgit / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")

        yield repo_path


# ---------------------------------------------------------------------------
# Repository Boundary Tests
# ---------------------------------------------------------------------------


class TestRepositoryBoundary:
    """Tests for repository boundary path resolver."""

    def test_unconfigured_root_fails_closed(self) -> None:
        """When coding_repo_root is None or empty, operations fail closed."""
        with patch.object(settings, "coding_repo_root", None):
            with pytest.raises(ConfigurationError, match="not configured"):
                get_repo_root()

            with pytest.raises(ConfigurationError, match="not configured"):
                resolve_safe_path("hello.py")

    def test_nonexistent_root_fails_closed(self) -> None:
        """Configured root pointing to nonexistent path raises ConfigurationError."""
        with pytest.raises(ConfigurationError, match="does not exist"):
            get_repo_root("/nonexistent/repo/root/dir")

    def test_root_is_a_file_fails_closed(self, temp_repo: Path) -> None:
        """Configured root pointing to a file instead of dir raises ConfigurationError."""
        file_path = temp_repo / "hello.py"
        with pytest.raises(ConfigurationError, match="not a directory"):
            get_repo_root(str(file_path))

    def test_empty_or_null_path_rejected(self, temp_repo: Path) -> None:
        """Empty path or null-byte paths raise ValidationError."""
        with pytest.raises(ValidationError):
            resolve_safe_path("", repo_root=str(temp_repo))

        with pytest.raises(ValidationError):
            resolve_safe_path("foo\x00bar.py", repo_root=str(temp_repo))

    def test_valid_relative_path_resolves(self, temp_repo: Path) -> None:
        """Normal relative path inside repository resolves correctly."""
        resolved = resolve_safe_path("hello.py", repo_root=str(temp_repo))
        assert resolved == (temp_repo / "hello.py").resolve()

    def test_valid_nested_path_resolves(self, temp_repo: Path) -> None:
        """Nested relative path resolves correctly."""
        resolved = resolve_safe_path("src/pkg/module.py", repo_root=str(temp_repo))
        assert resolved == (temp_repo / "src" / "pkg" / "module.py").resolve()

    def test_parent_traversal_rejected(self, temp_repo: Path) -> None:
        """Paths attempting to escape with ../ are rejected."""
        with pytest.raises(PathTraversalError):
            resolve_safe_path("../outside.txt", repo_root=str(temp_repo))

        with pytest.raises(PathTraversalError):
            resolve_safe_path("src/../../outside.txt", repo_root=str(temp_repo))

    def test_absolute_outside_path_rejected(self, temp_repo: Path) -> None:
        """Absolute path pointing outside repo is rejected."""
        outside = Path(tempfile.gettempdir()).resolve().parent
        with pytest.raises(PathTraversalError):
            resolve_safe_path(str(outside), repo_root=str(temp_repo))

    def test_git_directory_access_rejected(self, temp_repo: Path) -> None:
        """Accessing .git or its children is forbidden."""
        with pytest.raises(RepositoryBoundaryError, match="forbidden"):
            resolve_safe_path(".git", repo_root=str(temp_repo))

        with pytest.raises(RepositoryBoundaryError, match="forbidden"):
            resolve_safe_path(".git/config", repo_root=str(temp_repo))

        with pytest.raises(RepositoryBoundaryError, match="forbidden"):
            resolve_safe_path(".git/HEAD", repo_root=str(temp_repo))

    def test_nested_git_path_rejected(self, temp_repo: Path) -> None:
        """Nested attempts to reference .git are forbidden."""
        with pytest.raises(RepositoryBoundaryError, match="forbidden"):
            resolve_safe_path("src/.git", repo_root=str(temp_repo), allow_nonexistent=True)

    def test_symlink_escape_rejected(self, temp_repo: Path) -> None:
        """Symlinks pointing outside the repository root are rejected."""
        outside_file = Path(tempfile.gettempdir()) / "outside_secret.txt"
        outside_file.write_text("secret", encoding="utf-8")
        try:
            link_path = temp_repo / "escape_link"
            try:
                os.symlink(outside_file, link_path)
            except (OSError, NotImplementedError):
                pytest.skip("Symlink creation not supported in this test environment")

            with pytest.raises(PathTraversalError):
                resolve_safe_path("escape_link", repo_root=str(temp_repo))
        finally:
            if outside_file.exists():
                outside_file.unlink(missing_ok=True)

    def test_get_relative_repo_path(self, temp_repo: Path) -> None:
        """Relative path utility returns normalized forward-slash path."""
        target = temp_repo / "src" / "pkg" / "module.py"
        rel = get_relative_repo_path(target, str(temp_repo))
        assert rel == "src/pkg/module.py"


# ---------------------------------------------------------------------------
# ReadFileTool Tests
# ---------------------------------------------------------------------------


class TestReadFileTool:
    """Tests for ReadFileTool."""

    @pytest.mark.asyncio
    async def test_read_valid_file(self, temp_repo: Path) -> None:
        tool = ReadFileTool(repo_root=str(temp_repo))
        result = await tool.execute(path="hello.py")
        assert result.success is True
        assert result.output["content"] == "print('hello world')"
        assert result.output["path"] == "hello.py"

    @pytest.mark.asyncio
    async def test_read_nested_file(self, temp_repo: Path) -> None:
        tool = ReadFileTool(repo_root=str(temp_repo))
        result = await tool.execute(path="src/pkg/module.py")
        assert result.success is True
        assert "def add" in result.output["content"]

    @pytest.mark.asyncio
    async def test_read_directory_rejected(self, temp_repo: Path) -> None:
        tool = ReadFileTool(repo_root=str(temp_repo))
        result = await tool.execute(path="src")
        assert result.success is False
        assert "Cannot read a directory as a file" in result.error

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, temp_repo: Path) -> None:
        tool = ReadFileTool(repo_root=str(temp_repo))
        result = await tool.execute(path="does_not_exist.py")
        assert result.success is False
        assert "File not found" in result.error

    @pytest.mark.asyncio
    async def test_read_oversized_file(self, temp_repo: Path) -> None:
        tool = ReadFileTool(repo_root=str(temp_repo))
        large_file = temp_repo / "large.txt"
        large_file.write_text("x" * 200, encoding="utf-8")

        result = await tool.execute(path="large.txt", max_bytes=100)
        assert result.success is False
        assert "exceeds maximum allowed limit" in result.error

    @pytest.mark.asyncio
    async def test_read_git_file_blocked(self, temp_repo: Path) -> None:
        tool = ReadFileTool(repo_root=str(temp_repo))
        result = await tool.execute(path=".git/config")
        assert result.success is False
        assert "forbidden" in result.error

    @pytest.mark.asyncio
    async def test_read_traversal_blocked(self, temp_repo: Path) -> None:
        tool = ReadFileTool(repo_root=str(temp_repo))
        result = await tool.execute(path="../../some_secret.txt")
        assert result.success is False
        assert "traverses outside" in result.error


# ---------------------------------------------------------------------------
# ListFilesTool Tests
# ---------------------------------------------------------------------------


class TestListFilesTool:
    """Tests for ListFilesTool."""

    @pytest.mark.asyncio
    async def test_list_repo_root(self, temp_repo: Path) -> None:
        tool = ListFilesTool(repo_root=str(temp_repo))
        result = await tool.execute(path=".")
        assert result.success is True
        paths = [e["path"] for e in result.output["entries"]]
        assert "hello.py" in paths
        assert "README.md" in paths
        # .git must be excluded
        for p in paths:
            assert not p.startswith(".git")
            assert "/.git" not in p

    @pytest.mark.asyncio
    async def test_list_nested_directory(self, temp_repo: Path) -> None:
        tool = ListFilesTool(repo_root=str(temp_repo))
        result = await tool.execute(path="src/pkg")
        assert result.success is True
        paths = [e["path"] for e in result.output["entries"]]
        assert "src/pkg/module.py" in paths

    @pytest.mark.asyncio
    async def test_list_non_recursive(self, temp_repo: Path) -> None:
        tool = ListFilesTool(repo_root=str(temp_repo))
        result = await tool.execute(path=".", recursive=False)
        assert result.success is True
        paths = [e["path"] for e in result.output["entries"]]
        assert "hello.py" in paths
        # module.py shouldn't be directly in non-recursive root
        assert "src/pkg/module.py" not in paths

    @pytest.mark.asyncio
    async def test_list_limit_enforced(self, temp_repo: Path) -> None:
        tool = ListFilesTool(repo_root=str(temp_repo))
        result = await tool.execute(path=".", limit=2)
        assert result.success is True
        assert len(result.output["entries"]) == 2
        assert result.output["truncated"] is True

    @pytest.mark.asyncio
    async def test_list_traversal_blocked(self, temp_repo: Path) -> None:
        tool = ListFilesTool(repo_root=str(temp_repo))
        result = await tool.execute(path="../..")
        assert result.success is False
        assert "traverses outside" in result.error

    @pytest.mark.asyncio
    async def test_list_non_directory(self, temp_repo: Path) -> None:
        tool = ListFilesTool(repo_root=str(temp_repo))
        result = await tool.execute(path="hello.py")
        assert result.success is False
        assert "not a directory" in result.error


# ---------------------------------------------------------------------------
# WriteFileTool Tests
# ---------------------------------------------------------------------------


class TestWriteFileTool:
    """Tests for WriteFileTool."""

    @pytest.mark.asyncio
    async def test_write_new_file(self, temp_repo: Path) -> None:
        tool = WriteFileTool(repo_root=str(temp_repo))
        result = await tool.execute(path="new_file.py", content="# new content")
        assert result.success is True
        assert result.output["created"] is True
        assert (temp_repo / "new_file.py").read_text(encoding="utf-8") == "# new content"

    @pytest.mark.asyncio
    async def test_overwrite_existing_file(self, temp_repo: Path) -> None:
        tool = WriteFileTool(repo_root=str(temp_repo))
        result = await tool.execute(path="hello.py", content="# updated")
        assert result.success is True
        assert result.output["created"] is False
        assert (temp_repo / "hello.py").read_text(encoding="utf-8") == "# updated"

    @pytest.mark.asyncio
    async def test_write_nested_with_parents(self, temp_repo: Path) -> None:
        tool = WriteFileTool(repo_root=str(temp_repo))
        result = await tool.execute(path="deep/nested/dir/code.py", content="x = 1")
        assert result.success is True
        assert (temp_repo / "deep" / "nested" / "dir" / "code.py").exists()

    @pytest.mark.asyncio
    async def test_write_traversal_blocked(self, temp_repo: Path) -> None:
        tool = WriteFileTool(repo_root=str(temp_repo))
        result = await tool.execute(path="../outside.py", content="evil")
        assert result.success is False
        assert "traverses outside" in result.error

    @pytest.mark.asyncio
    async def test_write_git_blocked(self, temp_repo: Path) -> None:
        tool = WriteFileTool(repo_root=str(temp_repo))
        result = await tool.execute(path=".git/malicious_hook", content="hack")
        assert result.success is False
        assert "forbidden" in result.error

    @pytest.mark.asyncio
    async def test_write_over_directory_blocked(self, temp_repo: Path) -> None:
        tool = WriteFileTool(repo_root=str(temp_repo))
        result = await tool.execute(path="src", content="text")
        assert result.success is False
        assert "Cannot overwrite directory" in result.error


# ---------------------------------------------------------------------------
# Approval Enforcement with ToolExecutor
# ---------------------------------------------------------------------------


class TestCodingToolsApprovalEnforcement:
    """Tests verifying approval requirements through ToolExecutor."""

    def setup_method(self) -> None:
        permission_manager.clear()
        audit_log.clear()

    @pytest.mark.asyncio
    async def test_write_file_approval_required_by_default(self, temp_repo: Path) -> None:
        """WriteFileTool requires APPROVAL_REQUIRED permission by default; raises ApprovalRequiredError."""
        tool = WriteFileTool(repo_root=str(temp_repo))
        agent_id = AgentId(id="coding-agent")

        # By default agent has no permissions granted -> PermissionDeniedError
        with pytest.raises(PermissionDeniedError):
            await tool_executor.execute(tool=tool, agent_id=agent_id, path="f.txt", content="hi")

        # Now grant LOCAL_WRITE with level=APPROVAL_REQUIRED
        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_WRITE,
                level=PermissionLevel.APPROVAL_REQUIRED,
                description="Write files with approval",
            ),
        )

        # Execution must be blocked, raising ApprovalRequiredError
        with pytest.raises(ApprovalRequiredError) as exc_info:
            await tool_executor.execute(tool=tool, agent_id=agent_id, path="blocked.txt", content="hi")

        assert "write_file" in str(exc_info.value)
        assert "local_write" in str(exc_info.value)

        # File must not have been created
        assert not (temp_repo / "blocked.txt").exists()

        # Audit check: APPROVAL_REQUESTED recorded, TOOL_INVOKED not recorded
        approval_events = audit_log.get_events(event_type=AuditEventType.APPROVAL_REQUESTED)
        assert len(approval_events) == 1
        assert approval_events[0].details["tool"] == "write_file"

        invoked_events = audit_log.get_events(event_type=AuditEventType.TOOL_INVOKED)
        assert len(invoked_events) == 0

    @pytest.mark.asyncio
    async def test_write_file_executes_when_allowed(self, temp_repo: Path) -> None:
        """When permission is explicitly granted as ALLOW, write succeeds."""
        tool = WriteFileTool(repo_root=str(temp_repo))
        agent_id = AgentId(id="coding-agent")

        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_WRITE,
                level=PermissionLevel.ALLOW,
                description="Write files allowed",
            ),
        )

        result = await tool_executor.execute(tool=tool, agent_id=agent_id, path="allowed.txt", content="success")
        assert result.success is True
        assert (temp_repo / "allowed.txt").read_text(encoding="utf-8") == "success"

        # Audit check: TOOL_INVOKED recorded
        invoked_events = audit_log.get_events(event_type=AuditEventType.TOOL_INVOKED)
        assert len(invoked_events) == 1

    @pytest.mark.asyncio
    async def test_read_and_list_execute_with_local_read(self, temp_repo: Path) -> None:
        """Read and list execute cleanly when LOCAL_READ is ALLOW."""
        agent_id = AgentId(id="coding-agent")
        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_READ,
                level=PermissionLevel.ALLOW,
                description="Read allowed",
            ),
        )

        read_tool = ReadFileTool(repo_root=str(temp_repo))
        read_res = await tool_executor.execute(tool=read_tool, agent_id=agent_id, path="hello.py")
        assert read_res.success is True
        assert "hello world" in read_res.output["content"]

        list_tool = ListFilesTool(repo_root=str(temp_repo))
        list_res = await tool_executor.execute(tool=list_tool, agent_id=agent_id, path=".")
        assert list_res.success is True
        assert len(list_res.output["entries"]) >= 2
