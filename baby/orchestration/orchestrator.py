"""Main orchestrator for BABY.

Coordinates task planning, agent selection, execution, verification, and audit
logging while keeping execution bounded and deterministic.
"""

from typing import Optional

from baby.agents import agent_registry
from baby.audit import audit_log
from baby.core import AgentId, AgentResult, AuditEventType, ExecutionContext, Plan, PlanStep, Task, TaskStatus
from baby.errors import InvalidStateTransitionError
from baby.logging import get_logger
from baby.observability import task_tracker
from baby.planning.planner import task_planner
from baby.planning.selection import selection_logic

logger = get_logger(__name__)


class Orchestrator:
    """Orchestrate task planning and bounded agent execution."""

    async def execute_task(
        self,
        task: Task,
        user_id: Optional[str] = None,
    ) -> Optional[AgentResult]:
        """Execute a task end-to-end and return the final step result."""
        # Ensure task has user_id if provided
        if user_id and not task.user_id:
            task.user_id = user_id

        # Register as PENDING first, then transition to IN_PROGRESS following the state machine
        task_tracker.register_task(task, status=TaskStatus.PENDING)
        task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)

        logger.info(
            "Starting task execution",
            task_id=str(task.id),
            title=task.title,
            priority=task.priority,
        )
        audit_log.record(
            AuditEventType.TASK_CREATED,
            task_id=task.id,
            user_id=user_id or task.user_id,
            details={
                "title": task.title,
                "description": task.description,
                "priority": task.priority,
            },
        )

        try:
            plan = await task_planner.plan(task)
            task_tracker.set_plan(task.id, plan)
            audit_log.record(
                AuditEventType.PLAN_CREATED,
                task_id=task.id,
                user_id=user_id or task.user_id,
                details={"step_count": len(plan.steps), "reasoning": plan.reasoning},
            )

            result: Optional[AgentResult] = None
            for step in plan.steps:
                result = await self._execute_step(step, task, plan, user_id or task.user_id)
                if not result.success:
                    task_tracker.update_status(
                        task.id,
                        TaskStatus.FAILED,
                        error=result.error,
                        result=result,
                        agent_id=result.agent_id,
                    )
                    audit_log.record(
                        AuditEventType.TASK_FAILED,
                        task_id=task.id,
                        user_id=user_id or task.user_id,
                        details={"step_id": step.step_id, "error": result.error},
                    )
                    return result

            task_tracker.update_status(
                task.id,
                TaskStatus.COMPLETED,
                result=result,
                agent_id=result.agent_id if result else None,
            )
            audit_log.record(
                AuditEventType.TASK_COMPLETED,
                task_id=task.id,
                user_id=user_id or task.user_id,
                details={"steps_executed": len(plan.steps)},
            )
            return result
        except InvalidStateTransitionError:
            # State machine violation — log and re-raise without clobbering state
            logger.error(
                "Invalid task state transition during execution",
                task_id=str(task.id),
            )
            raise
        except Exception as exc:
            task_tracker.update_status(
                task.id,
                TaskStatus.FAILED,
                error=str(exc),
            )
            logger.error("Task execution failed", exc=exc, task_id=str(task.id))
            audit_log.record(
                AuditEventType.ERROR,
                task_id=task.id,
                user_id=user_id or task.user_id,
                details={"error": str(exc)},
            )
            raise

    async def _execute_step(
        self,
        step: PlanStep,
        task: Task,
        plan: Plan,
        user_id: Optional[str] = None,
    ) -> AgentResult:
        """Execute one planned step, returning a typed failure when unassigned."""
        del plan  # Reserved for dependency-aware execution in a later phase.
        agent_spec = await selection_logic.select_agent(step, task)
        if agent_spec is None:
            logger.warning("No enabled agent available", task_id=str(task.id), step_id=step.step_id)
            return AgentResult(
                step_id=step.step_id,
                agent_id=step.agent_id or AgentId(id="unassigned"),
                success=False,
                error="No enabled agent available",
            )

        audit_log.record(
            AuditEventType.AGENT_SELECTED,
            task_id=task.id,
            agent_id=agent_spec.id,
            user_id=user_id,
            details={"step_id": step.step_id},
        )

        agent = agent_registry.get(str(agent_spec.id))
        context = ExecutionContext(
            task_id=task.id,
            plan_step=step,
            task=task,
            agent=agent_spec,
        )
        try:
            return await agent.execute(context)
        except Exception as exc:
            logger.error("Step execution failed", exc=exc, task_id=str(task.id), step_id=step.step_id)
            return AgentResult(
                step_id=step.step_id,
                agent_id=agent_spec.id,
                success=False,
                error=str(exc),
            )


orchestrator = Orchestrator()
