"""Main orchestrator for BABY.

Coordinates task planning, agent selection, tool execution, and verification.
"""

from typing import Optional
from datetime import datetime

from baby.core import (
    Task,
    Plan,
    AgentResult,
    ExecutionContext,
    PlanStep,
    AuditEventType,
)
from baby.planning.planner import task_planner
from baby.planning.selection import selection_logic
from baby.agents import agent_registry
from baby.tools import tool_executor
from baby.audit import audit_log
from baby.logging import get_logger

logger = get_logger(__name__)


class Orchestrator:
    """Orchestrates task execution.
    
    Coordinates planning, agent/tool selection, execution, and verification.
    """

    async def execute_task(
        self,
        task: Task,
        user_id: Optional[str] = None,
    ) -> Optional[AgentResult]:
        """Execute a task end-to-end.
        
        Args:
            task: Task to execute
            user_id: User who authorized execution
            
        Returns:
            Final result or None if failed
        """
        logger.info(
            f"Starting task execution: {task.title}",
            task_id=str(task.id),
            priority=task.priority,
        )
        
        # Record task creation
        audit_log.record(
            AuditEventType.TASK_CREATED,
            task_id=task.id,
            user_id=user_id,
            details={
                "title": task.title,
                "description": task.description,
                "priority": task.priority,
            },
        )

        try:
            # Step 1: Plan the task
            plan = await task_planner.plan(task)
            audit_log.record(
                AuditEventType.PLAN_CREATED,
                task_id=task.id,
                user_id=user_id,
                details={
                    "step_count": len(plan.steps),
                    "reasoning": plan.reasoning,
                },
            )

            # Step 2: Execute each step
            result = None
            for step in plan.steps:
                result = await self._execute_step(
                    step=step,
                    task=task,
                    plan=plan,
                    user_id=user_id,
                )
                
                if not result.success:
                    logger.error(
                        f"Step failed, stopping execution",
                        task_id=str(task.id),
                        step_id=step.step_id,
                        error=result.error,
                    )
                    audit_log.record(
                        AuditEventType.TASK_FAILED,
                        task_id=task.id,
                        user_id=user_id,
                        details={
                            "step_id": step.step_id,
                            "error": result.error,
                        },
                    )
                    return result

            # Record successful completion
            logger.info(
                f"Task completed successfully",
                task_id=str(task.id),
            )
            audit_log.record(
                AuditEventType.TASK_COMPLETED,
                task_id=task.id,
                user_id=user_id,
                details={
                    "steps_executed": len(plan.steps),
                },
            )
            
            return result

        except Exception as e:
            logger.error(
                f"Task execution failed with exception",
                task_id=str(task.id),
                error=str(e),
            )
            audit_log.record(
                AuditEventType.ERROR,
                task_id=task.id,
                user_id=user_id,
                details={"error": str(e)},
            )
            raise

    async def _execute_step(
        self,
        step: PlanStep,
        task: Task,
        plan: Plan,
        user_id: Optional[str] = None,
    ) -> AgentResult:
        """Execute a single plan step.
        
        Args:
            step: Step to execute
            task: Associated task
            plan: Associated plan
            user_id: User who authorized
            
        Returns:
            Step result
        """
        logger.info(
            f"Executing step {step.step_id}",
            task_id=str(task.id),
            description=step.description,
        )
        
        # Select agent for this step
        agent_spec = await selection_logic.select_agent(step, task)
        if not agent_spec:
            logger.error("No agent available for step")
            return AgentResult(
                step_id=step.step_id,
                agent_id=step.agent_id or agent_registry.list_agents()[0].spec.id,
                success=False,
                error="No agent available",
            )
        
        audit_log.record(
            AuditEventType.AGENT_SELECTED,
            task_id=task.id,
            agent_id=agent_spec.id,
            user_id=user_id,
            details={"step_id": step.step_id},
        )

        # Get agent instance
        agent = agent_registry.get(str(agent_spec.id))
        
        # Create execution context
        context = ExecutionContext(
            task_id=task.id,
            plan_step=step,
            task=task,
            agent=agent_spec,
        )

        # Execute agent
        try:
            result = await agent.execute(context)
            logger.info(
                f"Step executed",
                task_id=str(task.id),
                step_id=step.step_id,
                success=result.success,
            )
            return result
        except Exception as e:
            logger.error(
                f"Step execution failed",
                task_id=str(task.id),
                step_id=step.step_id,
                error=str(e),
            )
            return AgentResult(
                step_id=step.step_id,
                agent_id=agent_spec.id,
                success=False,
                error=str(e),
            )


# Global orchestrator instance
orchestrator = Orchestrator()
