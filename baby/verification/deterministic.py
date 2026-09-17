"""Deterministic verification for agent results."""

from typing import Any

from baby.core import AgentResult, ExecutionContext, VerificationResult
from baby.errors import ToolNotFoundError
from baby.tools import SAFE_CODING_TOOL_NAMES, RepositoryTool, tool_registry


class DeterministicVerifier:
    """Verify results without relying on non-deterministic model output."""

    async def verify(self, result: AgentResult, context: ExecutionContext) -> VerificationResult:
        """Verify one agent result against its planned step."""
        issues: list[str] = []

        if not result.success:
            issues.append(result.error or "Agent execution failed")
        if result.step_id != context.plan_step.step_id:
            issues.append("Agent result step_id does not match the planned step")

        if context.plan_step.metadata.get("domain") == "coding":
            issues.extend(self._verify_coding_result(result.output, context))
        elif result.success and result.error:
            issues.append("Successful result should not include an error message")

        return VerificationResult(
            agent_result_id=result.step_id,
            agent_id=result.agent_id,
            verified=not issues,
            verification_method="deterministic",
            issues=issues,
        )

    def _verify_coding_result(self, output: Any, context: ExecutionContext) -> list[str]:
        issues: list[str] = []
        if not isinstance(output, dict):
            return ["Coding agent output must be a dictionary"]

        tool_name = output.get("tool")
        expected_tool_name = context.plan_step.tool_name
        if tool_name != expected_tool_name:
            issues.append("Coding agent output tool does not match the planned tool")
        if tool_name not in SAFE_CODING_TOOL_NAMES:
            issues.append("Coding agent output references a non-allow-listed tool")

        tool_result = output.get("result")
        if not isinstance(tool_result, dict):
            issues.append("Coding agent output must include structured tool result data")
            return issues

        if tool_name == "repository_list_files":
            files = tool_result.get("files")
            if not isinstance(files, list) or not all(isinstance(path, str) for path in files):
                issues.append("repository_list_files verification requires a list of string file paths")
        elif tool_name == "repository_read_file":
            if not isinstance(tool_result.get("content"), str):
                issues.append("repository_read_file verification requires text content")
        elif tool_name == "repository_write_file":
            bytes_written = tool_result.get("bytes_written")
            if not isinstance(bytes_written, int) or bytes_written < 0:
                issues.append("repository_write_file verification requires a non-negative bytes_written value")

        relative_path = tool_result.get("path")
        if isinstance(relative_path, str) and expected_tool_name in SAFE_CODING_TOOL_NAMES:
            try:
                tool = tool_registry.get(expected_tool_name)
            except ToolNotFoundError:
                issues.append(f"Verified tool is not registered: {expected_tool_name}")
            else:
                if isinstance(tool, RepositoryTool) and not tool._resolve_path(relative_path).exists():
                    issues.append(f"Verified path does not exist: {relative_path}")

        return issues


deterministic_verifier = DeterministicVerifier()
