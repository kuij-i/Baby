"""Tests for tool framework."""

import asyncio

import pytest

from baby.audit import audit_log
from baby.core import (
    AgentId,
    AuditEventType,
    PermissionCategory,
    PermissionLevel,
    TaskId,
    ToolPermission,
    ToolSpec,
)
from baby.errors import ApprovalRequiredError, PermissionDeniedError, ToolNotFoundError
from baby.permissions import approval_manager, permission_manager
from baby.tools import SAFE_CODING_TOOL_NAMES
from baby.tools.base import Tool, ToolResult
from baby.tools.coding import build_safe_coding_tools, register_safe_coding_tools
from baby.tools.executor import tool_executor
from baby.tools.registry import tool_registry


class MockTool(Tool):
    """Mock tool for testing."""

    async def execute(self, **kwargs) -> ToolResult:
        """Mock execution."""
        if kwargs.get("fail", False):
            return ToolResult(
                success=False,
                error="Mock tool failure",
                execution_time_ms=10,
            )
        return ToolResult(
            success=True,
            output={"result": "mock", "input": kwargs},
            execution_time_ms=5,
        )


class TestToolResult:
    """Tests for ToolResult."""

    def test_tool_result_success(self) -> None:
        """Test creating a successful tool result."""
        result = ToolResult(
            success=True,
            output={"data": "test"},
            execution_time_ms=10,
        )
        assert result.success is True
        assert result.error is None
        assert result.execution_time_ms == 10

    def test_tool_result_failure(self) -> None:
        """Test creating a failed tool result."""
        result = ToolResult(
            success=False,
            error="Tool failed",
            execution_time_ms=5,
        )
        assert result.success is False
        assert result.error == "Tool failed"


class TestTool:
    """Tests for Tool base class."""

    def create_test_tool(self, name: str) -> MockTool:
        """Helper to create a test tool."""
        perm = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read-only access",
        )
        spec = ToolSpec(
            name=name,
            description="A test tool",
            permissions_required=[perm],
            input_schema={"type": "object", "properties": {"data": {"type": "string"}}},
        )
        return MockTool(spec)

    def test_tool_creation(self) -> None:
        """Test creating a tool."""
        tool = self.create_test_tool("test-tool")
        assert tool.name == "test-tool"
        assert len(tool.permissions_required) == 1

    @pytest.mark.asyncio
    async def test_tool_execution(self) -> None:
        """Test executing a tool."""
        tool = self.create_test_tool("test-tool")
        result = await tool.execute(data="test")
        assert result.success is True
        assert result.output["input"]["data"] == "test"

    def test_tool_input_validation(self) -> None:
        """Test tool input validation."""
        tool = self.create_test_tool("test-tool")
        # Should not raise if optional input
        tool.validate_input()

    @pytest.mark.asyncio
    async def test_tool_retry(self) -> None:
        """Test tool execution with retry."""
        perm = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read-only",
        )
        spec = ToolSpec(
            name="retry-tool",
            description="Tool with retries",
            permissions_required=[perm],
            input_schema={},
            retry_count=2,
        )
        tool = MockTool(spec)
        result = await tool.execute_with_retry(fail=True)
        # After retries, still fails but with retry message
        assert result.success is False
        assert "3 attempts" in result.error


class TestToolRegistry:
    """Tests for ToolRegistry."""

    def setup_method(self) -> None:
        """Clear registry before each test."""
        tool_registry.clear()

    def create_test_tool(self, name: str) -> MockTool:
        """Helper to create a test tool."""
        perm = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read-only",
        )
        spec = ToolSpec(
            name=name,
            description="Test tool",
            permissions_required=[perm],
            input_schema={},
        )
        return MockTool(spec)

    def test_register_tool(self) -> None:
        """Test registering a tool."""
        tool = self.create_test_tool("tool-1")
        tool_registry.register(tool)

        retrieved = tool_registry.get("tool-1")
        assert retrieved.name == "tool-1"

    def test_register_duplicate_tool(self) -> None:
        """Test registering the same tool twice fails."""
        tool = self.create_test_tool("tool-1")
        tool_registry.register(tool)

        with pytest.raises(ValueError):
            tool_registry.register(tool)

    def test_get_nonexistent_tool(self) -> None:
        """Test getting a non-existent tool."""
        with pytest.raises(ToolNotFoundError):
            tool_registry.get("nonexistent")

    def test_unregister_tool(self) -> None:
        """Test unregistering a tool."""
        tool = self.create_test_tool("tool-1")
        tool_registry.register(tool)
        tool_registry.unregister("tool-1")

        with pytest.raises(ToolNotFoundError):
            tool_registry.get("tool-1")

    def test_list_tools(self) -> None:
        """Test listing all tools."""
        tool1 = self.create_test_tool("tool-1")
        tool2 = self.create_test_tool("tool-2")
        tool_registry.register(tool1)
        tool_registry.register(tool2)

        tools = tool_registry.list_tools()
        assert len(tools) == 2

    def test_get_tools_by_permission(self) -> None:
        """Test getting tools by permission."""
        perm_read = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read",
        )
        perm_write = ToolPermission(
            category=PermissionCategory.LOCAL_WRITE,
            level=PermissionLevel.ALLOW,
            description="Write",
        )

        spec1 = ToolSpec(
            name="tool-1",
            description="Test",
            permissions_required=[perm_read],
            input_schema={},
        )
        spec2 = ToolSpec(
            name="tool-2",
            description="Test",
            permissions_required=[perm_read, perm_write],
            input_schema={},
        )

        tool1 = MockTool(spec1)
        tool2 = MockTool(spec2)
        tool_registry.register(tool1)
        tool_registry.register(tool2)

        readers = tool_registry.get_tools_by_permission("read_only")
        assert len(readers) == 2

        writers = tool_registry.get_tools_by_permission("local_write")
        assert len(writers) == 1


class TestToolExecutor:
    """Tests for ToolExecutor."""

    def setup_method(self) -> None:
        """Setup test environment."""
        tool_registry.clear()
        permission_manager.clear()
        approval_manager.clear()
        audit_log.clear()

    @pytest.mark.asyncio
    async def test_execute_with_permission(self) -> None:
        """Test executing tool with permission."""
        # Create tool
        perm = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read-only",
        )
        spec = ToolSpec(
            name="test-tool",
            description="Test",
            permissions_required=[perm],
            input_schema={},
        )
        tool = MockTool(spec)
        tool_registry.register(tool)

        # Grant permission
        permission_manager.grant_permission("agent-1", perm)

        # Execute
        agent_id = AgentId(id="agent-1")
        result = await tool_executor.execute(
            tool=tool,
            agent_id=agent_id,
            data="test",
        )

        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_without_permission(self) -> None:
        """Test executing tool without permission."""
        perm = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read-only",
        )
        spec = ToolSpec(
            name="test-tool",
            description="Test",
            permissions_required=[perm],
            input_schema={},
        )
        tool = MockTool(spec)

        agent_id = AgentId(id="agent-1")
        with pytest.raises(PermissionDeniedError):
            await tool_executor.execute(
                tool=tool,
                agent_id=agent_id,
                data="test",
            )

    @pytest.mark.asyncio
    async def test_execute_with_timeout(self) -> None:
        """Test tool execution timeout."""
        perm = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read",
        )

        # Tool with very short timeout
        spec = ToolSpec(
            name="timeout-tool",
            description="Tool that times out",
            permissions_required=[perm],
            input_schema={},
            timeout_seconds=1,
        )

        class SlowTool(Tool):
            async def execute(self, **kwargs) -> ToolResult:
                await asyncio.sleep(2)  # Longer than timeout
                return ToolResult(success=True)

        tool = SlowTool(spec)
        permission_manager.grant_permission("agent-1", perm)

        agent_id = AgentId(id="agent-1")
        result = await tool_executor.execute(
            tool=tool,
            agent_id=agent_id,
        )

        assert result.success is False
        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_execute_approval_required_without_explicit_approval(self, tmp_path) -> None:
        """Test approval-gated tools cannot execute without approval."""
        register_safe_coding_tools(tmp_path)
        write_tool = tool_registry.get("repository_write_file")
        task_id = TaskId()
        agent_id = AgentId(id="agent-1")
        permission_manager.grant_permission(
            "agent-1",
            ToolPermission(
                category=PermissionCategory.LOCAL_WRITE,
                level=PermissionLevel.APPROVAL_REQUIRED,
                description="Write with approval",
            ),
        )

        with pytest.raises(ApprovalRequiredError):
            await tool_executor.execute(
                tool=write_tool,
                agent_id=agent_id,
                task_id=task_id,
                path="notes.txt",
                content="pending approval",
            )

        events = audit_log.get_events(task_id=task_id)
        event_types = [event.event_type for event in events]
        assert AuditEventType.PERMISSION_CHECKED in event_types
        assert AuditEventType.APPROVAL_REQUESTED in event_types

    @pytest.mark.asyncio
    async def test_execute_approval_granted_allows_write(self, tmp_path) -> None:
        """Test explicitly approved writes execute normally."""
        register_safe_coding_tools(tmp_path)
        write_tool = tool_registry.get("repository_write_file")
        task_id = TaskId()
        agent_id = AgentId(id="agent-1")
        permission_manager.grant_permission(
            "agent-1",
            ToolPermission(
                category=PermissionCategory.LOCAL_WRITE,
                level=PermissionLevel.APPROVAL_REQUIRED,
                description="Write with approval",
            ),
        )

        with pytest.raises(ApprovalRequiredError):
            await tool_executor.execute(
                tool=write_tool,
                agent_id=agent_id,
                task_id=task_id,
                path="notes.txt",
                content="approved content",
            )

        request = approval_manager.find_request(
            task_id=task_id,
            agent_id=agent_id,
            action_type="tool:repository_write_file:local_write",
        )
        assert request is not None
        approval_manager.decide(request.request_id, approved=True)

        result = await tool_executor.execute(
            tool=write_tool,
            agent_id=agent_id,
            task_id=task_id,
            path="notes.txt",
            content="approved content",
        )

        assert result.success is True
        assert (tmp_path / "notes.txt").read_text(encoding="utf-8") == "approved content"

    @pytest.mark.asyncio
    async def test_execute_approval_denied_blocks_write(self, tmp_path) -> None:
        """Test explicitly denied writes remain blocked."""
        register_safe_coding_tools(tmp_path)
        write_tool = tool_registry.get("repository_write_file")
        task_id = TaskId()
        agent_id = AgentId(id="agent-1")
        permission_manager.grant_permission(
            "agent-1",
            ToolPermission(
                category=PermissionCategory.LOCAL_WRITE,
                level=PermissionLevel.APPROVAL_REQUIRED,
                description="Write with approval",
            ),
        )

        with pytest.raises(ApprovalRequiredError):
            await tool_executor.execute(
                tool=write_tool,
                agent_id=agent_id,
                task_id=task_id,
                path="notes.txt",
                content="denied content",
            )

        request = approval_manager.find_request(
            task_id=task_id,
            agent_id=agent_id,
            action_type="tool:repository_write_file:local_write",
        )
        assert request is not None
        approval_manager.decide(request.request_id, approved=False)

        with pytest.raises(PermissionDeniedError):
            await tool_executor.execute(
                tool=write_tool,
                agent_id=agent_id,
                task_id=task_id,
                path="notes.txt",
                content="denied content",
            )


class TestSafeCodingTools:
    """Tests for repository-bounded safe coding tools."""

    def setup_method(self) -> None:
        tool_registry.clear()
        permission_manager.clear()
        approval_manager.clear()

    def test_safe_tool_allow_list_has_no_unrestricted_capabilities(self, tmp_path) -> None:
        """Test safe coding tools expose only repository-bounded read/write operations."""
        tools = build_safe_coding_tools(tmp_path)
        assert {tool.name for tool in tools} == SAFE_CODING_TOOL_NAMES
        for tool in tools:
            assert all(
                permission.category in {PermissionCategory.LOCAL_READ, PermissionCategory.LOCAL_WRITE}
                for permission in tool.permissions_required
            )

    @pytest.mark.asyncio
    async def test_read_tool_blocks_paths_outside_repo_root(self, tmp_path) -> None:
        """Test repository tools reject filesystem traversal outside the repository root."""
        register_safe_coding_tools(tmp_path)
        read_tool = tool_registry.get("repository_read_file")
        permission_manager.grant_permission(
            "agent-1",
            ToolPermission(
                category=PermissionCategory.LOCAL_READ,
                level=PermissionLevel.ALLOW,
                description="Read local files",
            ),
        )

        result = await tool_executor.execute(
            tool=read_tool,
            agent_id=AgentId(id="agent-1"),
            task_id=TaskId(),
            path="../outside.txt",
        )

        assert result.success is False
        assert "outside the repository root" in (result.error or "")
