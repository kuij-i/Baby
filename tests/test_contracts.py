"""Tests for domain contracts."""

import pytest
from datetime import datetime
from uuid import UUID

from baby.core import (
    AgentCapability,
    AgentId,
    AgentResult,
    AgentSpec,
    ApprovalRequest,
    Artifact,
    PermissionCategory,
    PermissionLevel,
    Plan,
    PlanStep,
    Task,
    TaskId,
    ToolPermission,
    ToolSpec,
    VerificationResult,
)


class TestTaskId:
    """Tests for TaskId."""

    def test_task_id_creation(self) -> None:
        """Test creating a TaskId."""
        task_id = TaskId()
        assert isinstance(task_id.id, UUID)

    def test_task_id_string_representation(self) -> None:
        """Test string representation of TaskId."""
        task_id = TaskId()
        assert str(task_id) == str(task_id.id)

    def test_task_id_hashable(self) -> None:
        """Test TaskId is hashable."""
        task_id1 = TaskId()
        task_id2 = TaskId()
        # Different IDs should have different hashes
        assert hash(task_id1) != hash(task_id2)


class TestAgentId:
    """Tests for AgentId."""

    def test_agent_id_creation(self) -> None:
        """Test creating an AgentId."""
        agent_id = AgentId(id="coding-agent-v1")
        assert agent_id.id == "coding-agent-v1"

    def test_agent_id_string_representation(self) -> None:
        """Test string representation of AgentId."""
        agent_id = AgentId(id="test-agent")
        assert str(agent_id) == "test-agent"

    def test_agent_id_validation_empty(self) -> None:
        """Test AgentId validation rejects empty strings."""
        with pytest.raises(ValueError):
            AgentId(id="")

    def test_agent_id_hashable(self) -> None:
        """Test AgentId is hashable."""
        agent_id1 = AgentId(id="agent-1")
        agent_id2 = AgentId(id="agent-1")
        # Same IDs should have same hash
        assert hash(agent_id1) == hash(agent_id2)


class TestPermissions:
    """Tests for permission types."""

    def test_permission_level_enum(self) -> None:
        """Test PermissionLevel enum values."""
        assert PermissionLevel.ALLOW.value == "allow"
        assert PermissionLevel.DENY.value == "deny"
        assert PermissionLevel.APPROVAL_REQUIRED.value == "approval_required"

    def test_permission_category_enum(self) -> None:
        """Test PermissionCategory enum has expected values."""
        assert PermissionCategory.READ_ONLY.value == "read_only"
        assert PermissionCategory.EXECUTE_COMMAND.value == "execute_command"
        assert PermissionCategory.FINANCIAL_ACTION.value == "financial_action"
        assert PermissionCategory.DESTRUCTIVE_ACTION.value == "destructive_action"

    def test_tool_permission_creation(self) -> None:
        """Test creating a ToolPermission."""
        perm = ToolPermission(
            category=PermissionCategory.READ_ONLY,
            level=PermissionLevel.ALLOW,
            description="Read-only file access",
        )
        assert perm.category == PermissionCategory.READ_ONLY
        assert perm.level == PermissionLevel.ALLOW
        assert perm.description == "Read-only file access"

    def test_tool_permission_with_constraints(self) -> None:
        """Test ToolPermission with constraints."""
        perm = ToolPermission(
            category=PermissionCategory.EXECUTE_COMMAND,
            level=PermissionLevel.APPROVAL_REQUIRED,
            description="Execute shell commands",
            constraints={"allowed_commands": ["ls", "grep"], "max_timeout": 30},
        )
        assert perm.constraints is not None
        assert "allowed_commands" in perm.constraints


class TestAgentCapability:
    """Tests for AgentCapability."""

    def test_agent_capability_creation(self) -> None:
        """Test creating an AgentCapability."""
        cap = AgentCapability(
            name="code-review",
            description="Review Python code for quality and security",
            required_permissions=[PermissionCategory.GITHUB_READ],
        )
        assert cap.name == "code-review"
        assert len(cap.required_permissions) == 1


class TestToolSpec:
    """Tests for ToolSpec."""

    def test_tool_spec_creation(self) -> None:
        """Test creating a ToolSpec."""
        perm = ToolPermission(
            category=PermissionCategory.GITHUB_READ,
            level=PermissionLevel.ALLOW,
            description="GitHub read access",
        )
        tool = ToolSpec(
            name="github-api",
            description="GitHub API client",
            permissions_required=[perm],
            input_schema={"type": "object", "properties": {"repo": {"type": "string"}}},
        )
        assert tool.name == "github-api"
        assert tool.version == "1.0.0"
        assert tool.timeout_seconds == 300


class TestAgentSpec:
    """Tests for AgentSpec."""

    def test_agent_spec_creation(self) -> None:
        """Test creating an AgentSpec."""
        agent_id = AgentId(id="coding-agent")
        cap = AgentCapability(
            name="code-review",
            description="Review code",
        )
        agent = AgentSpec(
            id=agent_id,
            name="Coding Agent",
            description="An AI agent for coding tasks",
            role="software-engineer",
            capabilities=[cap],
        )
        assert agent.id == agent_id
        assert agent.name == "Coding Agent"
        assert agent.model == "gpt-4"
        assert agent.enabled is True


class TestTask:
    """Tests for Task."""

    def test_task_creation(self) -> None:
        """Test creating a Task."""
        task = Task(
            title="Implement authentication",
            description="Add OAuth2 authentication to the API",
        )
        assert task.title == "Implement authentication"
        assert isinstance(task.id, TaskId)
        assert isinstance(task.created_at, datetime)

    def test_task_priority_validation(self) -> None:
        """Test Task priority validation."""
        # Valid priorities
        task = Task(title="Test", description="Test", priority=5)
        assert task.priority == 5

        # Invalid: too high
        with pytest.raises(ValueError):
            Task(title="Test", description="Test", priority=11)

        # Invalid: negative
        with pytest.raises(ValueError):
            Task(title="Test", description="Test", priority=-1)


class TestPlanStep:
    """Tests for PlanStep."""

    def test_plan_step_creation(self) -> None:
        """Test creating a PlanStep."""
        agent_id = AgentId(id="agent-1")
        step = PlanStep(
            step_id=1,
            description="Review the pull request",
            agent_id=agent_id,
            tool_name="github-api",
        )
        assert step.step_id == 1
        assert step.agent_id == agent_id
        assert step.approval_required is False

    def test_plan_step_with_dependencies(self) -> None:
        """Test PlanStep with dependencies."""
        step = PlanStep(
            step_id=2,
            description="Deploy to staging",
            dependencies=[1],  # Depends on step 1
        )
        assert step.dependencies == [1]


class TestPlan:
    """Tests for Plan."""

    def test_plan_creation(self) -> None:
        """Test creating a Plan."""
        task_id = TaskId()
        step = PlanStep(step_id=1, description="Step 1")
        plan = Plan(
            task_id=task_id,
            steps=[step],
            reasoning="This plan will accomplish the task by executing one step",
        )
        assert plan.task_id == task_id
        assert len(plan.steps) == 1

    def test_plan_requires_at_least_one_step(self) -> None:
        """Test that Plan requires at least one step."""
        task_id = TaskId()
        with pytest.raises(ValueError):
            Plan(
                task_id=task_id,
                steps=[],
                reasoning="No steps",
            )


class TestAgentResult:
    """Tests for AgentResult."""

    def test_agent_result_success(self) -> None:
        """Test creating a successful AgentResult."""
        agent_id = AgentId(id="agent-1")
        result = AgentResult(
            step_id=1,
            agent_id=agent_id,
            success=True,
            output={"status": "completed"},
            tokens_used=150,
        )
        assert result.success is True
        assert result.error is None
        assert result.tokens_used == 150

    def test_agent_result_failure(self) -> None:
        """Test creating a failed AgentResult."""
        agent_id = AgentId(id="agent-1")
        result = AgentResult(
            step_id=1,
            agent_id=agent_id,
            success=False,
            error="API timeout",
        )
        assert result.success is False
        assert result.error == "API timeout"


class TestVerificationResult:
    """Tests for VerificationResult."""

    def test_verification_result_verified(self) -> None:
        """Test creating a verified VerificationResult."""
        result = VerificationResult(
            agent_result_id=1,
            verified=True,
            verification_method="test-execution",
        )
        assert result.verified is True
        assert len(result.issues) == 0

    def test_verification_result_with_issues(self) -> None:
        """Test VerificationResult with issues."""
        result = VerificationResult(
            agent_result_id=1,
            verified=False,
            verification_method="test-execution",
            issues=["Test 1 failed", "Test 2 failed"],
        )
        assert result.verified is False
        assert len(result.issues) == 2


class TestApprovalRequest:
    """Tests for ApprovalRequest."""

    def test_approval_request_creation(self) -> None:
        """Test creating an ApprovalRequest."""
        task_id = TaskId()
        agent_id = AgentId(id="agent-1")
        request = ApprovalRequest(
            task_id=task_id,
            agent_id=agent_id,
            action_type="delete-database",
            reason="User requested deletion of test database",
            risk_level="high",
        )
        assert request.approved is None
        assert request.approval_timestamp is None


class TestArtifact:
    """Tests for Artifact."""

    def test_artifact_creation(self) -> None:
        """Test creating an Artifact."""
        task_id = TaskId()
        agent_id = AgentId(id="agent-1")
        artifact = Artifact(
            task_id=task_id,
            agent_id=agent_id,
            name="generated-code.py",
            artifact_type="source-code",
            content="def hello(): pass",
        )
        assert artifact.name == "generated-code.py"
        assert artifact.verified is False
