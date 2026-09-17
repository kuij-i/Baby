"""Tool executor with permission and timeout enforcement."""

import asyncio
from typing import Any, Optional

from baby.audit import audit_log
from baby.core import AgentId, AuditEventType, PermissionLevel, TaskId
from baby.errors import ApprovalRequiredError, ExecutionError, PermissionDeniedError
from baby.logging import get_logger
from baby.permissions import approval_manager, permission_manager
from baby.tools.base import Tool, ToolResult

logger = get_logger(__name__)


class ToolExecutor:
    """Executes tools with permission checks, timeouts, and audit logging."""

    async def execute(
        self,
        tool: Tool,
        agent_id: AgentId,
        task_id: Optional[TaskId] = None,
        user_id: Optional[str] = None,
        **kwargs: Any,
    ) -> ToolResult:
        """Execute a tool with all safety checks.

        Args:
            tool: Tool to execute
            agent_id: Agent executing the tool
            task_id: Associated task ID
            user_id: User who authorized execution
            **kwargs: Tool input arguments

        Returns:
            ToolResult with execution outcome

        Raises:
            PermissionDeniedError: If agent lacks required permissions
            ExecutionError: If execution fails
        """
        agent_id_str = str(agent_id)

        try:
            # 1. Validate input
            tool.validate_input(**kwargs)
            logger.debug(
                "Tool input validated",
                tool=tool.name,
                agent_id=agent_id_str,
            )

            # 2. Check permissions
            for required_perm in tool.permissions_required:
                try:
                    level = permission_manager.check_permission(agent_id_str, required_perm)
                    logger.info(
                        "Permission checked",
                        tool=tool.name,
                        agent_id=agent_id_str,
                        permission=required_perm.category.value,
                        level=level.value,
                    )
                    audit_log.record(
                        AuditEventType.PERMISSION_CHECKED,
                        task_id=task_id,
                        agent_id=agent_id,
                        user_id=user_id,
                        details={
                            "tool": tool.name,
                            "permission": required_perm.category.value,
                            "result": level.value,
                        },
                    )
                    if level == PermissionLevel.APPROVAL_REQUIRED:
                        if task_id is None:
                            raise ApprovalRequiredError(
                                "Tool "
                                f"{tool.name} requires approval for {required_perm.category.value} "
                                "but no task was provided"
                            )
                        action_type = f"tool:{tool.name}:{required_perm.category.value}"
                        approval = approval_manager.find_request(
                            task_id=task_id,
                            agent_id=agent_id,
                            action_type=action_type,
                        )
                        if approval is None:
                            approval = approval_manager.request_approval(
                                task_id=task_id,
                                agent_id=agent_id,
                                action_type=action_type,
                                reason=f"Execute {tool.name} with {required_perm.category.value}",
                                risk_level="high",
                                user_id=user_id,
                            )
                        if approval.approved is True:
                            continue
                        if approval.approved is False:
                            raise PermissionDeniedError(
                                f"Approval denied for {tool.name} using {required_perm.category.value}"
                            )
                        raise ApprovalRequiredError(
                            "Approval required for "
                            f"{tool.name} using {required_perm.category.value}: {approval.request_id}"
                        )
                except PermissionDeniedError as e:
                    logger.warning(
                        "Permission denied",
                        tool=tool.name,
                        agent_id=agent_id_str,
                        error=str(e),
                    )
                    audit_log.record(
                        AuditEventType.PERMISSION_CHECKED,
                        task_id=task_id,
                        agent_id=agent_id,
                        user_id=user_id,
                        details={
                            "tool": tool.name,
                            "permission": required_perm.category.value,
                            "result": "denied",
                            "reason": str(e),
                        },
                    )
                    raise
                except ApprovalRequiredError as e:
                    logger.info(
                        "Approval required",
                        tool=tool.name,
                        agent_id=agent_id_str,
                        error=str(e),
                    )
                    raise

            # 3. Record tool invocation
            audit_log.record(
                AuditEventType.TOOL_INVOKED,
                task_id=task_id,
                agent_id=agent_id,
                user_id=user_id,
                details={"tool": tool.name, "input_keys": list(kwargs.keys())},
            )

            # 4. Execute with timeout
            try:
                result = await asyncio.wait_for(
                    tool.execute_with_retry(**kwargs),
                    timeout=tool.spec.timeout_seconds,
                )
            except asyncio.TimeoutError:
                error_msg = f"Tool {tool.name} timed out after {tool.spec.timeout_seconds}s"
                logger.error(error_msg, tool=tool.name, agent_id=agent_id_str)
                result = ToolResult(
                    success=False,
                    error=error_msg,
                    execution_time_ms=tool.spec.timeout_seconds * 1000,
                )

            # 5. Record tool result
            audit_log.record(
                AuditEventType.TOOL_RESULT,
                task_id=task_id,
                agent_id=agent_id,
                user_id=user_id,
                details={
                    "tool": tool.name,
                    "success": result.success,
                    "execution_time_ms": result.execution_time_ms,
                    "error": result.error,
                },
            )

            if result.success:
                logger.info(
                    "Tool executed successfully",
                    tool=tool.name,
                    agent_id=agent_id_str,
                    execution_time_ms=result.execution_time_ms,
                )
            else:
                logger.warning(
                    "Tool execution failed",
                    tool=tool.name,
                    agent_id=agent_id_str,
                    error=result.error,
                )

            return result

        except (PermissionDeniedError, ApprovalRequiredError):
            raise
        except Exception as e:
            logger.error(
                "Tool execution error",
                tool=tool.name,
                agent_id=agent_id_str,
                error=str(e),
            )
            raise ExecutionError(f"Tool {tool.name} execution failed: {str(e)}") from e


# Global tool executor instance
tool_executor = ToolExecutor()
