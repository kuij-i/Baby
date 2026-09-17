"""Tests for deterministic verification."""

import pytest

from baby.agents import register_coding_agent
from baby.core import (
    AgentId,
    AgentResult,
    ExecutionContext,
    PermissionCategory,
    PermissionLevel,
    PlanStep,
    Task,
    ToolPermission,
)
from baby.permissions import approval_manager, permission_manager
from baby.tools import register_safe_coding_tools, tool_registry
from baby.verification import deterministic_verifier


class TestDeterministicVerifier:
    """Tests for deterministic result verification."""

    def setup_method(self) -> None:
        tool_registry.clear()
        permission_manager.clear()
        approval_manager.clear()

    @pytest.mark.asyncio
    async def test_verification_success_for_coding_read_result(self, tmp_path) -> None:
        """Test successful coding results verify cleanly."""
        (tmp_path / "README.md").write_text("hello", encoding="utf-8")
        register_safe_coding_tools(tmp_path)
        permission_manager.grant_permission(
            "coding-agent",
            ToolPermission(
                category=PermissionCategory.LOCAL_READ,
                level=PermissionLevel.ALLOW,
                description="Read repository files",
            ),
        )
        agent = register_coding_agent(tmp_path)
        task = Task(title="Read code", description="Read a file", metadata={"domain": "coding"})
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

        verification = await deterministic_verifier.verify(result, context)

        assert verification.verified is True
        assert verification.issues == []

    @pytest.mark.asyncio
    async def test_verification_failure_reports_issues(self, tmp_path) -> None:
        """Test invalid coding outputs fail deterministic verification."""
        agent = register_coding_agent(tmp_path)
        task = Task(title="Write code", description="Write a file", metadata={"domain": "coding"})
        context = ExecutionContext(
            task_id=task.id,
            task=task,
            agent=agent.spec,
            plan_step=PlanStep(
                step_id=1,
                description="Write notes",
                tool_name="repository_write_file",
                metadata={"domain": "coding", "tool_input": {"path": "notes.txt", "content": "hello"}},
            ),
        )
        result = AgentResult(
            step_id=1,
            agent_id=AgentId(id="coding-agent"),
            success=True,
            output={"tool": "repository_write_file", "result": {"path": "missing.txt", "bytes_written": -1}},
        )

        verification = await deterministic_verifier.verify(result, context)

        assert verification.verified is False
        assert any("non-negative bytes_written" in issue for issue in verification.issues)
        assert any("does not exist" in issue for issue in verification.issues)
