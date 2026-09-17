"""Main orchestrator for BABY.

Coordinates task planning, bounded agent execution, explicit verification, and
audit logging while keeping execution deterministic.
"""

from typing import Optional

from baby.agents import agent_registry
from baby.audit import audit_log
from baby.core import AgentId, AgentResult, AuditEventType, ExecutionContext, Plan, PlanStep, Task
from baby.logging import get_logger
from baby.planning.planner import task_planner
from baby.planning.selection import selection_logic
from baby.verification import Verifier, verifier

logger = get_logger(__name__)


class Orchestrator:
    """Orchestrate task planning and bounded agent execution."""

    def __init__(self, verification_boundary: Verifier = verifier) -> None:
        self._verification_boundary = verification_boundary

    async def execute_task(
        self,
        task: Task,
        user_id: Optional[str] = None,
    ) -> Optional[AgentResult]:
        """Execute a task end-to-end and return the final step result."""
        logger.info(
            "Starting task execution",
            task_id=str(task.id),
            title=task.title,
            priority=task.priority,
        )
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
            plan = await task_planner.plan(task)
            audit_log.record(
                AuditEventType.PLAN_CREATED,
                task_id=task.id,
                user_id=user_id,
                details={"step_count": len(plan.steps), "reasoning": plan.reasoning},
            )
            self._validate_plan_dependencies(plan)

            result: Optional[AgentResult] = None
            pending_steps = {step.step_id: step for step in plan.steps}
            completed_results: dict[int, AgentResult] = {}
            while pending_steps:
                ready_steps = [
                    step
                    for step in plan.steps
                    if step.step_id in pending_steps
                    and all(dependency in completed_results for dependency in step.dependencies)
                ]
                if not ready_steps:
                    raise ValueError("Circular dependency detected in plan execution")

                step = ready_steps[0]
                dependency_results = {dependency: completed_results[dependency] for dependency in step.dependencies}
                failed_dependencies = [
                    dependency
                    for dependency, dependency_result in dependency_results.items()
                    if not dependency_result.success
                ]
                if failed_dependencies:
                    result = AgentResult(
                        step_id=step.step_id,
                        agent_id=step.agent_id or AgentId(id="dependency-blocked"),
                        success=False,
                        error=f"Blocked by failed dependencies: {failed_dependencies}",
                    )
                else:
                    result = await self._execute_step(
                        step,
                        task,
                        plan,
                        dependency_results,
                        user_id,
                    )
                completed_results[step.step_id] = result
                del pending_steps[step.step_id]
                if not result.success:
                    audit_log.record(
                        AuditEventType.TASK_FAILED,
                        task_id=task.id,
                        user_id=user_id,
                        details={"step_id": step.step_id, "error": result.error},
                    )
                    return result

            audit_log.record(
                AuditEventType.TASK_COMPLETED,
                task_id=task.id,
                user_id=user_id,
                details={"steps_executed": len(plan.steps)},
            )
            return result
        except Exception as exc:
            logger.error("Task execution failed", exc=exc, task_id=str(task.id))
            audit_log.record(
                AuditEventType.ERROR,
                task_id=task.id,
                user_id=user_id,
                details={"error": str(exc)},
            )
            raise

    async def _execute_step(
        self,
        step: PlanStep,
        task: Task,
        plan: Plan,
        dependency_results: dict[int, AgentResult],
        user_id: Optional[str] = None,
    ) -> AgentResult:
        """Execute one planned step, returning a typed failure when unassigned."""
        del plan
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
            previous_results=dependency_results,
        )
        try:
            result = await agent.execute(context)
            verification = await self._verification_boundary.verify(result)
            audit_log.record(
                AuditEventType.VERIFICATION_COMPLETED,
                task_id=task.id,
                agent_id=agent_spec.id,
                user_id=user_id,
                details={
                    "step_id": step.step_id,
                    "agent_result_id": str(verification.agent_result_id),
                    "verified": verification.verified,
                    "verification_method": verification.verification_method,
                    "issues": verification.issues,
                },
            )
            if verification.verified:
                return result
            if not result.success:
                return result
            return result.model_copy(
                update={
                    "success": False,
                    "error": "Verification failed: " + "; ".join(verification.issues),
                }
            )
        except Exception as exc:
            logger.error("Step execution failed", exc=exc, task_id=str(task.id), step_id=step.step_id)
            return AgentResult(
                step_id=step.step_id,
                agent_id=agent_spec.id,
                success=False,
                error=str(exc),
            )

    def _validate_plan_dependencies(self, plan: Plan) -> None:
        """Reject missing, duplicate, and circular plan dependencies."""
        steps_by_id = {step.step_id: step for step in plan.steps}
        if len(steps_by_id) != len(plan.steps):
            raise ValueError("Plan contains duplicate step IDs")

        for step in plan.steps:
            missing_dependencies = [dependency for dependency in step.dependencies if dependency not in steps_by_id]
            if missing_dependencies:
                raise ValueError(f"Step {step.step_id} has invalid dependencies: {missing_dependencies}")

        visiting: set[int] = set()
        visited: set[int] = set()

        def visit(step_id: int) -> None:
            if step_id in visited:
                return
            if step_id in visiting:
                raise ValueError(f"Circular dependency detected at step {step_id}")

            visiting.add(step_id)
            for dependency in steps_by_id[step_id].dependencies:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step in plan.steps:
            visit(step.step_id)


orchestrator = Orchestrator()
