"""Tool executor with permission and timeout enforcement."""

import asyncio
from typing import TYPE_CHECKING, Any, Optional

from baby.audit import audit_log
from baby.core import AgentId, AuditEventType, PermissionLevel, TaskId
from baby.errors import ApprovalRequiredError, ExecutionError, PermissionDeniedError
from baby.logging import get_logger
from baby.permissions import permission_manager
from baby.permissions.approvals import approval_service
from baby.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from baby.audit import AuditLog
    from baby.permissions.approvals import ApprovalService
    from baby.permissions.manager import PermissionManager

logger = get_logger(__name__)


class ToolExecutor:
    """Executes tools with permission checks, timeouts, and audit logging."""

    def __init__(
        self,
        *,
        permissions: Optional["PermissionManager"] = None,
        audit: Optional["AuditLog"] = None,
        approvals: Optional["ApprovalService"] = None,
    ) -> None:
        """Initialize the executor.

        Each dependency defaults to the process-wide singleton, resolved when it is used.

        Args:
            permissions: Permission manager consulted for every required permission.
            audit: Audit log that records permission checks, invocations and results.
            approvals: Approval service for APPROVAL_REQUIRED permissions.
        """
        self._permissions = permissions
        self._audit = audit
        self._approvals = approvals

    @property
    def permissions(self) -> "PermissionManager":
        """Permission manager in use."""
        return self._permissions if self._permissions is not None else permission_manager

    @property
    def audit(self) -> "AuditLog":
        """Audit log in use."""
        return self._audit if self._audit is not None else audit_log

    @property
    def approvals(self) -> "ApprovalService":
        """Approval service in use."""
        return self._approvals if self._approvals is not None else approval_service

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
            ApprovalRequiredError: If tool requires approval that has not been granted
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
                    level = self.permissions.check_permission(agent_id_str, required_perm)
                    if level == PermissionLevel.APPROVAL_REQUIRED:
                        # Only an admin-granted, unconsumed approval bound to this exact task,
                        # engagement and operation (tool + agent + permission + arguments)
                        # lets execution proceed; it is consumed atomically (single use).
                        granted = self.approvals.authorize_or_request(
                            task_id=task_id,
                            agent_id=agent_id,
                            engagement_id=user_id,
                            operation=f"tool:{tool.name}",
                            permission=required_perm.category.value,
                            arguments=kwargs,
                            reason=f"Tool '{tool.name}' requires approval for '{required_perm.category.value}'",
                        )
                        if granted:
                            self.audit.record(
                                AuditEventType.APPROVAL_GRANTED,
                                task_id=task_id,
                                agent_id=agent_id,
                                user_id=user_id,
                                details={
                                    "tool": tool.name,
                                    "permission": required_perm.category.value,
                                    "result": "approval_consumed",
                                },
                            )
                            continue
                        logger.warning(
                            "Approval required — tool execution blocked",
                            tool=tool.name,
                            agent_id=agent_id_str,
                            permission=required_perm.category.value,
                        )
                        self.audit.record(
                            AuditEventType.APPROVAL_REQUESTED,
                            task_id=task_id,
                            agent_id=agent_id,
                            user_id=user_id,
                            details={
                                "tool": tool.name,
                                "permission": required_perm.category.value,
                                "result": "approval_required",
                            },
                        )
                        raise ApprovalRequiredError(
                            f"Tool '{tool.name}' requires approval for "
                            f"permission '{required_perm.category.value}' "
                            f"(agent: {agent_id_str})"
                        )

                    logger.info(
                        "Permission checked",
                        tool=tool.name,
                        agent_id=agent_id_str,
                        permission=required_perm.category.value,
                        level=level.value,
                    )
                except PermissionDeniedError as e:
                    logger.warning(
                        "Permission denied",
                        tool=tool.name,
                        agent_id=agent_id_str,
                        error=str(e),
                    )
                    self.audit.record(
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

            # 3. Record tool invocation
            self.audit.record(
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
            self.audit.record(
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
