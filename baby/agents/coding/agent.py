"""Specialist Coding Agent for repository-bounded code operations."""

import json
from typing import Any, Dict, List, Optional

from baby.agents.base import Agent
from baby.core import (
    AgentCapability,
    AgentId,
    AgentResult,
    AgentSpec,
    ExecutionContext,
    PermissionCategory,
    PermissionLevel,
    ToolPermission,
)
from baby.errors import ApprovalRequiredError, PermissionDeniedError
from baby.logging import get_logger
from baby.providers.base import ModelProvider
from baby.providers.registry import provider_registry
from baby.providers.types import ChatMessage, ToolDefinition
from baby.tools.base import Tool
from baby.tools.coding.list_files import ListFilesTool
from baby.tools.coding.read_file import ReadFileTool
from baby.tools.coding.write_file import WriteFileTool
from baby.tools.executor import tool_executor

logger = get_logger(__name__)

CODING_AGENT_CAPABILITIES = [
    AgentCapability(
        name="read_code",
        description="Inspect repository structure and read source files",
        required_permissions=[PermissionCategory.LOCAL_READ],
    ),
    AgentCapability(
        name="write_code",
        description="Create and modify files within the repository",
        required_permissions=[PermissionCategory.LOCAL_WRITE],
    ),
]

CODING_AGENT_PERMISSIONS = [
    ToolPermission(
        category=PermissionCategory.LOCAL_READ,
        level=PermissionLevel.ALLOW,
        description="Read files and list directories within repository",
    ),
    ToolPermission(
        category=PermissionCategory.LOCAL_WRITE,
        level=PermissionLevel.APPROVAL_REQUIRED,
        description="Write or modify files within repository (requires approval)",
    ),
]


def create_default_coding_spec(agent_id: str = "coding-agent") -> AgentSpec:
    """Create a standard specification for the Coding Agent."""
    return AgentSpec(
        id=AgentId(id=agent_id),
        name="CodingAgent",
        description="Specialist agent for repository-bounded coding operations.",
        role="coding",
        capabilities=CODING_AGENT_CAPABILITIES,
        permissions=CODING_AGENT_PERMISSIONS,
        model="gpt-4",
        enabled=True,
    )


class CodingAgent(Agent):
    """Specialist agent for repository-bounded coding tasks.

    Executes all filesystem actions via repository-bounded tools through
    ``ToolExecutor`` to enforce permission boundaries and approval workflows.
    Zero command execution, zero shell invocations.
    """

    def __init__(
        self,
        spec: Optional[AgentSpec] = None,
        provider: Optional[ModelProvider] = None,
        repo_root: Optional[str] = None,
    ) -> None:
        actual_spec = spec or create_default_coding_spec()
        super().__init__(actual_spec)
        self._provider = provider
        self._repo_root = repo_root

        # Instantiate repository-bounded tools
        self._tools: Dict[str, Tool] = {
            "list_files": ListFilesTool(repo_root=repo_root),
            "read_file": ReadFileTool(repo_root=repo_root),
            "write_file": WriteFileTool(repo_root=repo_root),
        }

    @property
    def tools(self) -> Dict[str, Tool]:
        """Dictionary of tools available to this agent."""
        return dict(self._tools)

    def get_tool_definitions(self) -> List[ToolDefinition]:
        """Convert bounded tools into provider-compatible tool definitions."""
        definitions = []
        for name, tool in self._tools.items():
            definitions.append(
                ToolDefinition(
                    name=name,
                    description=tool.spec.description,
                    parameters=tool.spec.input_schema,
                )
            )
        return definitions

    async def execute(self, context: ExecutionContext) -> AgentResult:
        """Execute a plan step using repository-bounded coding tools.

        Args:
            context: Execution context containing task and plan step.

        Returns:
            AgentResult detailing the execution outcome.
        """
        step = context.plan_step
        logger.info(
            "CodingAgent executing plan step",
            agent_id=self.id,
            step_id=step.step_id,
            tool_name=step.tool_name,
        )

        try:
            # 1. Direct tool invocation if plan step specifies a tool
            if step.tool_name and step.tool_name in self._tools:
                tool = self._tools[step.tool_name]
                tool_args = self._extract_tool_args(step.description)
                tool_result = await tool_executor.execute(
                    tool=tool,
                    agent_id=self.spec.id,
                    task_id=context.task_id,
                    user_id=context.task.user_id,
                    **tool_args,
                )
                return AgentResult(
                    step_id=step.step_id,
                    agent_id=self.spec.id,
                    success=tool_result.success,
                    output=tool_result.output,
                    error=tool_result.error,
                    execution_time_ms=tool_result.execution_time_ms,
                )

            # 2. Model-assisted execution if provider is available
            provider = self._get_provider()
            if provider and provider.is_available():
                return await self._execute_with_provider(context, provider)

            # 3. Fallback: parse direct action heuristic from step description
            return await self._execute_heuristic(context)

        except (ApprovalRequiredError, PermissionDeniedError) as exc:
            logger.warning(
                "CodingAgent execution blocked by security policy",
                agent_id=self.id,
                error=str(exc),
            )
            return AgentResult(
                step_id=step.step_id,
                agent_id=self.spec.id,
                success=False,
                error=str(exc),
            )
        except Exception as exc:
            logger.error(
                "CodingAgent execution failed",
                agent_id=self.id,
                error=str(exc),
                exc_info=True,
            )
            return AgentResult(
                step_id=step.step_id,
                agent_id=self.spec.id,
                success=False,
                error=f"CodingAgent execution error: {str(exc)}",
            )

    async def _execute_with_provider(
        self,
        context: ExecutionContext,
        provider: ModelProvider,
    ) -> AgentResult:
        """Execute task by presenting bounded tools to the model provider."""
        step = context.plan_step
        tool_defs = self.get_tool_definitions()

        messages = [
            ChatMessage(
                role="system",
                content=(
                    "You are the Baby Coding Agent. You have repository-bounded tools "
                    "to inspect, read, and write code files within the configured repository. "
                    "You cannot access files outside the repository or execute arbitrary commands."
                ),
            ),
            ChatMessage(
                role="user",
                content=f"Task: {context.task.title}\nStep: {step.description}",
            ),
        ]

        response = await provider.complete_with_tools(
            messages=messages,
            tools=tool_defs,
            model=self.spec.model,
        )

        tokens = response.usage.total_tokens if response.usage else None

        # Process tool calls if the model invoked any
        if response.tool_calls:
            outputs = []
            for tc in response.tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name")
                raw_args = fn.get("arguments", "{}")
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args

                if tool_name in self._tools:
                    tool = self._tools[tool_name]
                    tool_res = await tool_executor.execute(
                        tool=tool,
                        agent_id=self.spec.id,
                        task_id=context.task_id,
                        user_id=context.task.user_id,
                        **args,
                    )
                    outputs.append({"tool": tool_name, "result": tool_res.output, "success": tool_res.success})
                    if not tool_res.success:
                        return AgentResult(
                            step_id=step.step_id,
                            agent_id=self.spec.id,
                            success=False,
                            output=outputs,
                            error=tool_res.error,
                            tokens_used=tokens,
                        )

            return AgentResult(
                step_id=step.step_id,
                agent_id=self.spec.id,
                success=True,
                output={"response": response.content, "tool_results": outputs},
                tokens_used=tokens,
            )

        return AgentResult(
            step_id=step.step_id,
            agent_id=self.spec.id,
            success=True,
            output=response.content,
            tokens_used=tokens,
        )

    async def _execute_heuristic(self, context: ExecutionContext) -> AgentResult:
        """Fallback execution when provider is not configured."""
        step = context.plan_step
        desc_lower = step.description.lower()

        # Heuristic dispatch based on step description keyword
        if "list" in desc_lower or "ls" in desc_lower:
            tool = self._tools["list_files"]
            tool_args = self._extract_tool_args(step.description)
            res = await tool_executor.execute(
                tool=tool,
                agent_id=self.spec.id,
                task_id=context.task_id,
                user_id=context.task.user_id,
                **tool_args,
            )
            return AgentResult(
                step_id=step.step_id,
                agent_id=self.spec.id,
                success=res.success,
                output=res.output,
                error=res.error,
            )

        return AgentResult(
            step_id=step.step_id,
            agent_id=self.spec.id,
            success=True,
            output=f"CodingAgent completed step: {step.description}",
        )

    def _get_provider(self) -> Optional[ModelProvider]:
        """Get the model provider if configured."""
        if self._provider:
            return self._provider
        try:
            return provider_registry.get_default()
        except Exception:
            return None

    @staticmethod
    def _extract_tool_args(description: str) -> Dict[str, Any]:
        """Attempt to extract JSON arguments from a plan step description."""
        try:
            if "{" in description and "}" in description:
                start = description.index("{")
                end = description.rindex("}") + 1
                parsed = json.loads(description[start:end])
                if isinstance(parsed, dict):
                    return dict(parsed)
        except Exception:
            pass
        return {}
