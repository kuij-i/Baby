"""Main orchestrator for BABY."""

from typing import Optional

from baby.agents import agent_registry
from baby.audit import audit_log
from baby.core import AgentId, AgentResult, AuditEventType, ExecutionContext, MemoryRecord, Plan, PlanStep, Task
from baby.logging import get_logger
from baby.memory import memory_store
from baby.planning.planner import task_planner
from baby.planning.selection import selection_logic
from baby.verification import deterministic_verifier

logger = get_logger(__name__)


class Orchestrator:
    """Orchestrate task planning and bounded agent execution."""

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
            self._validate_plan(plan)

            result: Optional[AgentResult] = None
            completed_results: dict[int, AgentResult] = {}
            for step in plan.steps:
                dependency_error = self._dependency_error(step, completed_results)
                if dependency_error is not None:
                    result = AgentResult(
                        step_id=step.step_id,
                        agent_id=step.agent_id or AgentId(id="dependency-blocked"),
                        success=False,
                        error=dependency_error,
                    )
                else:
                    result = await self._execute_step(
                        step,
                        task,
                        plan,
                        {dependency: completed_results[dependency] for dependency in step.dependencies},
                        user_id,
                    )
                completed_results[step.step_id] = result
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
        previous_results: dict[int, AgentResult],
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
            previous_results=previous_results,
        )
        try:
            result = await agent.execute(context)
            verification = await deterministic_verifier.verify(result, context)
            audit_log.record(
                AuditEventType.VERIFICATION_COMPLETED,
                task_id=task.id,
                agent_id=agent_spec.id,
                user_id=user_id,
                details={
                    "step_id": step.step_id,
                    "verified": verification.verified,
                    "verification_method": verification.verification_method,
                    "issues": verification.issues,
                },
            )
            if verification.verified and result.success:
                self._store_safe_memory(result, context)
                return result

            return AgentResult(
                step_id=result.step_id,
                agent_id=result.agent_id,
                success=False,
                output=result.output,
                error=result.error or "; ".join(verification.issues) or "Verification failed",
                execution_time_ms=result.execution_time_ms,
                tokens_used=result.tokens_used,
            )
        except Exception as exc:
            logger.error("Step execution failed", exc=exc, task_id=str(task.id), step_id=step.step_id)
            return AgentResult(
                step_id=step.step_id,
                agent_id=agent_spec.id,
                success=False,
                error=str(exc),
            )

    @staticmethod
    def _validate_plan(plan: Plan) -> None:
        step_ids = [step.step_id for step in plan.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Plan contains duplicate step IDs")

        known_step_ids = set(step_ids)
        for step in plan.steps:
            for dependency in step.dependencies:
                if dependency not in known_step_ids:
                    raise ValueError(f"Step {step.step_id} depends on unknown step {dependency}")

        adjacency = {step.step_id: step.dependencies for step in plan.steps}
        visited: set[int] = set()
        visiting: set[int] = set()

        def visit(step_id: int) -> None:
            if step_id in visited:
                return
            if step_id in visiting:
                raise ValueError("Plan contains circular step dependencies")
            visiting.add(step_id)
            for dependency in adjacency[step_id]:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step_id in step_ids:
            visit(step_id)

    @staticmethod
    def _dependency_error(step: PlanStep, completed_results: dict[int, AgentResult]) -> Optional[str]:
        for dependency in step.dependencies:
            result = completed_results[dependency]
            if not result.success:
                return f"Dependency step {dependency} failed"
        return None

    @staticmethod
    def _store_safe_memory(result: AgentResult, context: ExecutionContext) -> None:
        if context.plan_step.metadata.get("domain") != "coding" or not isinstance(result.output, dict):
            return
        memory_store.save(
            MemoryRecord(
                task_id=context.task_id,
                agent_id=context.agent.id,
                record_type="coding_action",
                content={
                    "tool": result.output.get("tool"),
                    "paths": result.output.get("paths", []),
                    "used_previous_results": result.output.get("used_previous_results", []),
                },
            )
        )


orchestrator = Orchestrator()
