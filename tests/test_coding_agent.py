"""Tests for the secure coding agent."""

import pytest

from baby.agents import CODING_AGENT_ID, CODING_CAPABILITY, register_coding_agent
from baby.core import (
    ExecutionContext,
    PermissionCategory,
    PermissionLevel,
    PlanStep,
    Task,
    ToolPermission,
)
from baby.permissions import approval_manager, permission_manager
from baby.tools import register_safe_coding_tools, tool_registry


class TestCodingAgent:
    """Tests for CodingAgent."""

    def setup_method(self) -> None:
        tool_registry.clear()
        permission_manager.clear()
        approval_manager.clear()

    def test_coding_agent_creation(self, tmp_path) -> None:
        """Test the default coding agent metadata."""
        agent = register_coding_agent(tmp_path)
        assert agent.id == CODING_AGENT_ID
        assert agent.role == "coding"
        assert any(capability.name == CODING_CAPABILITY for capability in agent.spec.capabilities)

    @pytest.mark.asyncio
    async def test_coding_agent_executes_read_step(self, tmp_path) -> None:
        """Test the coding agent executes safe read steps through the Agent contract."""
        target = tmp_path / "README.md"
        target.write_text("hello", encoding="utf-8")
        register_safe_coding_tools(tmp_path)
        permission_manager.grant_permission(
            CODING_AGENT_ID,
            ToolPermission(
                category=PermissionCategory.LOCAL_READ,
                level=PermissionLevel.ALLOW,
                description="Read repository files",
            ),
        )
        agent = register_coding_agent(tmp_path)
        task = Task(
            title="Read repository file",
            description="Read a file for a coding task",
            metadata={"domain": "coding"},
        )
        context = ExecutionContext(
            task_id=task.id,
            task=task,
            agent=agent.spec,
            plan_step=PlanStep(
                step_id=1,
                description="Read README",
                tool_name="repository_read_file",
                metadata={"domain": "coding", "tool_input": {"path": "README.md"}},
            ),
        )

        result = await agent.execute(context)

        assert result.success is True
        assert result.output["tool"] == "repository_read_file"
        assert result.output["result"]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_coding_agent_rejects_non_allow_listed_tool(self, tmp_path) -> None:
        """Test the coding agent refuses tools outside the explicit allow list."""
        agent = register_coding_agent(tmp_path)
        task = Task(title="Bad tool", description="Attempt an unsafe tool", metadata={"domain": "coding"})
        context = ExecutionContext(
            task_id=task.id,
            task=task,
            agent=agent.spec,
            plan_step=PlanStep(
                step_id=1,
                description="Unsafe step",
                tool_name="shell",
                metadata={"domain": "coding", "tool_input": {"command": "ls"}},
            ),
        )

        result = await agent.execute(context)

        assert result.success is False
        assert "not allow-listed" in (result.error or "")
