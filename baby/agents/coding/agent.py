"""Specialist Coding Agent for repository-bounded code operations."""

import json
import re
from typing import Any, Dict, List, Optional

from baby.agents.base import Agent
from baby.audit import audit_log
from baby.configuration import settings
from baby.core import (
    AgentCapability,
    AgentId,
    AgentResult,
    AgentSpec,
    AuditEventType,
    ExecutionContext,
    PermissionCategory,
    PermissionLevel,
    ToolPermission,
)
from baby.errors import ApprovalRequiredError, PermissionDeniedError
from baby.logging import get_logger
from baby.providers.base import ModelProvider
from baby.providers.registry import configured_model, provider_registry
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

# The only phrasings the CodingAgent runs without a model provider, matched against the WHOLE description.
# A path is one token of path characters (no spaces, quotes or shell metacharacters) that cannot start with "-".
_LIST_REQUEST_RE = re.compile(
    r"(?:please )?(?:ls|list(?: the| all(?: the)?)? (?:files|directory|dir))"
    r"(?: (?:(?:in|under|of) )?(?P<path>[A-Za-z0-9_./][A-Za-z0-9_./-]*))?",
    re.IGNORECASE,
)
_LIST_PREPOSITIONS = {"in", "under", "of"}


def _parse_list_request(description: str) -> Optional[Dict[str, Any]]:
    """Parse an explicit file-listing request into ``list_files`` arguments.

    Without a model provider this is the only free-text request the CodingAgent may run, so it accepts a small,
    strict set of phrasings ("ls [path]", "list files [in <path>]", "list the directory", "please list files", ...)
    matched against the whole normalised description, optionally followed by a trailing JSON object of tool
    arguments (``list files {"path": "src"}``). Anything else, e.g. "list the bugs in auth.py", returns None.
    The returned path is not validated here: it must still go through ``ToolExecutor`` and the path resolver.

    Args:
        description: Plan step description.

    Returns:
        Keyword arguments for ``list_files`` (empty for the repository root), or None if the description is not
        an explicit file-listing request.
    """
    text = description.strip()
    json_args: Dict[str, Any] = {}
    if "{" in text:
        start = text.index("{")
        try:
            parsed = json.loads(text[start:])
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        json_args = parsed
        text = text[:start]

    prefix = " ".join(text.split())
    match = _LIST_REQUEST_RE.fullmatch(prefix) or _LIST_REQUEST_RE.fullmatch(prefix.rstrip(".!?"))
    if match is None:
        return None

    path = match.group("path")
    if path is None:
        return json_args
    if set(path) - {".", "/"}:
        # Trailing dots after a real path name are sentence punctuation ("list files in src.").
        path = path.rstrip(".")
    if path.lower() in _LIST_PREPOSITIONS or "path" in json_args:
        # "list files in" with no path, or a path given both in text and JSON: ambiguous, fail closed.
        return None
    return {**json_args, "path": path}


def create_default_coding_spec(agent_id: str = "coding-agent") -> AgentSpec:
    """Create a standard specification for the Coding Agent."""
    return AgentSpec(
        id=AgentId(id=agent_id),
        name="CodingAgent",
        description="Specialist agent for repository-bounded coding operations.",
        role="coding",
        capabilities=CODING_AGENT_CAPABILITIES,
        permissions=CODING_AGENT_PERMISSIONS,
        model=configured_model(),
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
        max_iterations: Optional[int] = None,
        max_tool_calls_per_iteration: Optional[int] = None,
    ) -> None:
        actual_spec = spec or create_default_coding_spec()
        super().__init__(actual_spec)
        self._provider = provider
        self._repo_root = repo_root
        self._max_iterations = max_iterations
        self._max_tool_calls_per_iteration = max_tool_calls_per_iteration

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
        """Execute task by presenting bounded tools to the model provider in an iterative loop."""
        step = context.plan_step
        tool_defs = self.get_tool_definitions()
        max_iterations = self._max_iterations or settings.coding_max_iterations
        max_tool_calls_per_iteration = (
            self._max_tool_calls_per_iteration or settings.coding_max_tool_calls_per_iteration
        )

        messages: List[ChatMessage] = [
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

        all_tool_outputs: List[Dict[str, Any]] = []
        total_tokens = 0

        for iteration in range(1, max_iterations + 1):
            logger.info(
                "CodingAgent starting iteration",
                agent_id=self.id,
                iteration=iteration,
                max_iterations=max_iterations,
            )

            try:
                response = await provider.complete_with_tools(
                    messages=messages,
                    tools=tool_defs,
                    model=self.spec.model,
                )
            except Exception as exc:
                logger.error(
                    "Model provider completion failed",
                    agent_id=self.id,
                    iteration=iteration,
                    error=str(exc),
                )
                return AgentResult(
                    step_id=step.step_id,
                    agent_id=self.spec.id,
                    success=False,
                    output={"tool_results": all_tool_outputs, "iterations": iteration},
                    error=f"Model provider error: {str(exc)}",
                    tokens_used=total_tokens or None,
                )

            if response.usage and response.usage.total_tokens:
                total_tokens += response.usage.total_tokens

            # If model proposes no tool calls, it has completed the step
            if not response.tool_calls:
                logger.info(
                    "CodingAgent completed without further tool calls",
                    agent_id=self.id,
                    iteration=iteration,
                )
                return AgentResult(
                    step_id=step.step_id,
                    agent_id=self.spec.id,
                    success=True,
                    output={
                        "response": response.content,
                        "tool_results": all_tool_outputs,
                        "iterations": iteration,
                    },
                    tokens_used=total_tokens or None,
                )

            # Enforce per-iteration tool-call limit to prevent uncontrolled execution
            if len(response.tool_calls) > max_tool_calls_per_iteration:
                error_msg = (
                    f"Model requested {len(response.tool_calls)} tool calls in iteration {iteration}, "
                    f"exceeding per-iteration limit of {max_tool_calls_per_iteration}"
                )
                logger.warning(error_msg, agent_id=self.id)
                return AgentResult(
                    step_id=step.step_id,
                    agent_id=self.spec.id,
                    success=False,
                    output={"tool_results": all_tool_outputs, "iterations": iteration},
                    error=error_msg,
                    tokens_used=total_tokens or None,
                )

            # Record assistant message with tool calls in conversation history
            messages.append(
                ChatMessage(
                    role="assistant",
                    content=response.content or "",
                    tool_calls=response.tool_calls,
                )
            )

            # Execute tool calls through ToolExecutor
            for idx, tc in enumerate(response.tool_calls):
                fn = tc.get("function", {})
                tool_name = fn.get("name")
                tool_call_id = tc.get("id") or f"call_{iteration}_{idx}"

                if not tool_name or tool_name not in self._tools:
                    error_msg = f"Unknown or missing tool requested by model: '{tool_name}'"
                    logger.warning(error_msg, agent_id=self.id)
                    return AgentResult(
                        step_id=step.step_id,
                        agent_id=self.spec.id,
                        success=False,
                        output={"tool_results": all_tool_outputs, "iterations": iteration},
                        error=error_msg,
                        tokens_used=total_tokens or None,
                    )

                raw_args = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        args = {}
                except Exception as exc:
                    error_msg = f"Malformed arguments for tool '{tool_name}': {str(exc)}"
                    logger.warning(error_msg, agent_id=self.id)
                    return AgentResult(
                        step_id=step.step_id,
                        agent_id=self.spec.id,
                        success=False,
                        output={"tool_results": all_tool_outputs, "iterations": iteration},
                        error=error_msg,
                        tokens_used=total_tokens or None,
                    )

                tool = self._tools[tool_name]
                try:
                    tool_res = await tool_executor.execute(
                        tool=tool,
                        agent_id=self.spec.id,
                        task_id=context.task_id,
                        user_id=context.task.user_id,
                        **args,
                    )
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
                        output={"tool_results": all_tool_outputs, "iterations": iteration},
                        error=str(exc),
                        tokens_used=total_tokens or None,
                    )

                all_tool_outputs.append(
                    {
                        "tool": tool_name,
                        "result": tool_res.output,
                        "success": tool_res.success,
                        "iteration": iteration,
                    }
                )

                if not tool_res.success:
                    logger.warning(
                        "Tool execution returned failure",
                        tool=tool_name,
                        error=tool_res.error,
                        iteration=iteration,
                    )
                    return AgentResult(
                        step_id=step.step_id,
                        agent_id=self.spec.id,
                        success=False,
                        output={"tool_results": all_tool_outputs, "iterations": iteration},
                        error=tool_res.error,
                        tokens_used=total_tokens or None,
                    )

                # Feed successful result back into conversation for next iteration
                tool_content = (
                    json.dumps(tool_res.output)
                    if isinstance(tool_res.output, (dict, list))
                    else str(tool_res.output or "")
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        content=tool_content,
                        name=tool_name,
                        tool_call_id=tool_call_id,
                    )
                )

        # Iteration limit reached without model conclusion
        error_msg = f"CodingAgent reached maximum iteration limit of {max_iterations} without completing task"
        logger.warning(error_msg, agent_id=self.id, max_iterations=max_iterations)
        audit_log.record(
            AuditEventType.ERROR,
            task_id=context.task_id,
            agent_id=self.spec.id,
            user_id=context.task.user_id,
            details={
                "error_type": "iteration_limit_exceeded",
                "iterations": max_iterations,
                "message": error_msg,
            },
        )
        return AgentResult(
            step_id=step.step_id,
            agent_id=self.spec.id,
            success=False,
            output={
                "tool_results": all_tool_outputs,
                "iterations": max_iterations,
            },
            error=error_msg,
            tokens_used=total_tokens or None,
        )

    async def _execute_heuristic(self, context: ExecutionContext) -> AgentResult:
        """Fallback execution when no model provider is available.

        Only an explicit file-listing request (see ``_parse_list_request``) can be handled without a
        model; it runs ``list_files`` through the executor, so path and repository boundary checks
        still apply. Anything else fails explicitly: the agent must never report a step as completed
        when nothing was executed.
        """
        step = context.plan_step
        tool_args = _parse_list_request(step.description)

        if tool_args is not None:
            tool = self._tools["list_files"]
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

        logger.warning("CodingAgent cannot execute step without a model provider", agent_id=self.id)
        return AgentResult(
            step_id=step.step_id,
            agent_id=self.spec.id,
            success=False,
            error=(
                "No model provider is configured (set NVIDIA_API_KEY or OPENAI_API_KEY to enable the model-driven "
                "CodingAgent). Without one only explicit tool steps and explicit 'list files' requests can run; "
                "nothing was executed for this step."
            ),
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
