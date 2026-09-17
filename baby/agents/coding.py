"""Secure coding agent implementation."""

from pathlib import Path
from typing import Any

from baby.agents.base import Agent
from baby.agents.registry import AgentRegistry, agent_registry
from baby.core import AgentCapability, AgentId, AgentResult, AgentSpec, ExecutionContext
from baby.errors import ApprovalRequiredError, PermissionDeniedError
from baby.tools import SAFE_CODING_TOOL_NAMES, register_safe_coding_tools, tool_executor, tool_registry

CODING_AGENT_ID = "coding-agent"
CODING_CAPABILITY = "coding_task_execution"


class CodingAgent(Agent):
    """Execute repository-bounded coding tasks through explicitly registered tools."""

    def __init__(self, spec: AgentSpec, repo_root: str | Path) -> None:
        super().__init__(spec)
        self.repo_root = Path(repo_root).resolve()

    async def execute(self, context: ExecutionContext) -> AgentResult:
        """Execute a coding step with a safe registered tool only."""
        tool_name = context.plan_step.tool_name
        if tool_name is None:
            return AgentResult(
                step_id=context.plan_step.step_id,
                agent_id=context.agent.id,
                success=False,
                error="No safe coding tool selected for the coding step",
            )

        if tool_name not in SAFE_CODING_TOOL_NAMES:
            return AgentResult(
                step_id=context.plan_step.step_id,
                agent_id=context.agent.id,
                success=False,
                error=f"Tool {tool_name} is not allow-listed for the coding agent",
            )

        tool = tool_registry.get(tool_name)
        tool_input = context.plan_step.metadata.get("tool_input", {})
        try:
            result = await tool_executor.execute(
                tool=tool,
                agent_id=context.agent.id,
                task_id=context.task_id,
                user_id=context.task.user_id,
                **tool_input,
            )
        except (ApprovalRequiredError, PermissionDeniedError) as exc:
            return AgentResult(
                step_id=context.plan_step.step_id,
                agent_id=context.agent.id,
                success=False,
                error=str(exc),
                output={"tool": tool_name},
            )

        if not result.success:
            return AgentResult(
                step_id=context.plan_step.step_id,
                agent_id=context.agent.id,
                success=False,
                error=result.error,
                output={"tool": tool_name},
                execution_time_ms=result.execution_time_ms,
            )

        self.set_metadata("last_tool", tool_name)
        output = {
            "tool": tool_name,
            "result": result.output,
            "paths": _extract_paths(result.output),
            "used_previous_results": sorted(context.previous_results),
        }
        return AgentResult(
            step_id=context.plan_step.step_id,
            agent_id=context.agent.id,
            success=True,
            output=output,
            execution_time_ms=result.execution_time_ms,
        )

def build_coding_agent_spec() -> AgentSpec:
    """Build the default coding agent specification."""
    return AgentSpec(
        id=AgentId(id=CODING_AGENT_ID),
        name="Coding Agent",
        description="Secure repository-bounded coding agent",
        role="coding",
        capabilities=[
            AgentCapability(
                name=CODING_CAPABILITY,
                description="Execute deterministic coding tasks through safe tools",
            )
        ],
    )


def register_coding_agent(
    repo_root: str | Path,
    registry: AgentRegistry | None = None,
) -> CodingAgent:
    """Register the default coding agent and its safe tools."""
    register_safe_coding_tools(repo_root)
    active_registry = registry or agent_registry
    existing = next((agent for agent in active_registry.list_agents() if agent.id == CODING_AGENT_ID), None)
    if existing is not None and isinstance(existing, CodingAgent):
        return existing

    agent = CodingAgent(build_coding_agent_spec(), repo_root=repo_root)
    active_registry.register(agent)
    return agent


def _extract_paths(result_output: Any) -> list[str]:
    """Return safe repository paths from tool output."""
    if isinstance(result_output, dict):
        if "path" in result_output and isinstance(result_output["path"], str):
            return [result_output["path"]]
        if "files" in result_output and isinstance(result_output["files"], list):
            return [path for path in result_output["files"] if isinstance(path, str)]
    return []
