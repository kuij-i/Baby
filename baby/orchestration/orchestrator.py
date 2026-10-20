"""Main orchestrator for BABY.

Coordinates task planning, agent selection, execution, verification, and audit
logging while keeping execution bounded and deterministic.
"""

from typing import TYPE_CHECKING, Any, Dict, Optional

from baby.agents import agent_registry
from baby.audit import audit_log
from baby.core import AgentId, AgentResult, AuditEventType, ExecutionContext, Plan, PlanStep, Task, TaskStatus
from baby.errors import InvalidStateTransitionError
from baby.logging import get_logger
from baby.observability import task_tracker
from baby.observability.redaction import redact_sensitive_data
from baby.planning.planner import task_planner
from baby.planning.selection import selection_logic

if TYPE_CHECKING:
    from baby.agents.registry import AgentRegistry
    from baby.audit import AuditLog
    from baby.observability.tasks import TaskTracker
    from baby.permissions.approvals import ApprovalService
    from baby.planning.planner import TaskPlanner
    from baby.planning.selection import SelectionLogic

logger = get_logger(__name__)

ORCHESTRATOR_AGENT_ID = AgentId(id="orchestrator")
PLAN_APPROVAL_OPERATION = "plan"
PLAN_APPROVAL_PERMISSION = "plan_approval"


def plan_requires_approval(plan: Plan) -> bool:
    """True if the whole plan needs one admin approval before any step runs.

    ``PlanStep.approval_required`` is honoured too, so plans stored before plan-level approvals
    existed still require approval.
    """
    return plan.approval_required or any(step.approval_required for step in plan.steps)


def plan_approval_arguments(plan: Plan, priority: int) -> Dict[str, Any]:
    """Arguments a plan approval is bound to: the full ordered step list and the task priority."""
    return {"steps": [step.description for step in plan.steps], "priority": priority}


def _plan_created_details(plan: Plan) -> Dict[str, Any]:
    details = {"step_count": len(plan.steps), "planner_mode": plan.planner_mode, "reasoning": plan.reasoning}
    return dict(redact_sensitive_data(details))


class Orchestrator:
    """Orchestrate task planning and bounded agent execution."""

    def __init__(
        self,
        *,
        task_tracker: Optional["TaskTracker"] = None,
        audit: Optional["AuditLog"] = None,
        planner: Optional["TaskPlanner"] = None,
        selection: Optional["SelectionLogic"] = None,
        agents: Optional["AgentRegistry"] = None,
        approvals: Optional["ApprovalService"] = None,
    ) -> None:
        """Initialize the orchestrator.

        Each dependency defaults to the process-wide singleton, resolved when it is used (so a
        ``BabyContext`` can wire its own services while existing call sites keep working).

        Args:
            task_tracker: Task state tracker.
            audit: Audit log.
            planner: Task planner.
            selection: Agent selection logic.
            agents: Agent registry steps are dispatched to.
            approvals: Approval service for plan approvals.
        """
        self._task_tracker = task_tracker
        self._audit = audit
        self._planner = planner
        self._selection = selection
        self._agents = agents
        self._approvals = approvals

    @property
    def task_tracker(self) -> "TaskTracker":
        """Task tracker in use."""
        return self._task_tracker if self._task_tracker is not None else task_tracker

    @property
    def audit(self) -> "AuditLog":
        """Audit log in use."""
        return self._audit if self._audit is not None else audit_log

    @property
    def planner(self) -> "TaskPlanner":
        """Task planner in use."""
        return self._planner if self._planner is not None else task_planner

    @property
    def selection(self) -> "SelectionLogic":
        """Selection logic in use."""
        return self._selection if self._selection is not None else selection_logic

    @property
    def agents(self) -> "AgentRegistry":
        """Agent registry in use."""
        return self._agents if self._agents is not None else agent_registry

    @property
    def approvals(self) -> "ApprovalService":
        """Approval service in use."""
        if self._approvals is not None:
            return self._approvals
        from baby.permissions.approvals import approval_service

        return approval_service

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
        self.task_tracker.register_task(task, status=TaskStatus.PENDING)
        self.task_tracker.update_status(task.id, TaskStatus.IN_PROGRESS)

        logger.info(
            "Starting task execution",
            task_id=str(task.id),
            title=task.title,
            priority=task.priority,
        )
        self.audit.record(
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
            plan = await self.planner.plan(task)
            self.task_tracker.set_plan(task.id, plan)
            self.audit.record(
                AuditEventType.PLAN_CREATED,
                task_id=task.id,
                user_id=user_id or task.user_id,
                details=_plan_created_details(plan),
            )

            result = await self._run_steps(plan, task, user_id or task.user_id)
            if result is not None and not result.success:
                self.task_tracker.update_status(
                    task.id,
                    TaskStatus.FAILED,
                    error=result.error,
                    result=result,
                    agent_id=result.agent_id,
                )
                self.audit.record(
                    AuditEventType.TASK_FAILED,
                    task_id=task.id,
                    user_id=user_id or task.user_id,
                    details={"step_id": result.step_id, "error": result.error},
                )
                return result

            self.task_tracker.update_status(
                task.id,
                TaskStatus.COMPLETED,
                result=result,
                agent_id=result.agent_id if result else None,
            )
            self.audit.record(
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
            self.task_tracker.update_status(
                task.id,
                TaskStatus.FAILED,
                error=str(exc),
            )
            logger.error("Task execution failed", exc=exc, task_id=str(task.id))
            self.audit.record(
                AuditEventType.ERROR,
                task_id=task.id,
                user_id=user_id or task.user_id,
                details={"error": str(exc)},
            )
            raise

    async def execute_claimed_task(self, task: Task) -> Optional[AgentResult]:
        """Plan and execute a task that a durable worker has already claimed.

        Used by worker processes (``baby.workers``). Unlike ``execute_task`` this method
        performs no task status transitions: the worker owns them and writes them only
        while it still holds the task's lease (fenced writes), so a worker that lost its
        claim can never record a result.

        The plan is created once and stored on the task; a task resumed after an approval
        pause reuses its stored plan (the planner is not called again) and re-runs it from
        step 1, so its steps and approval digests never change.

        A plan that requires approval runs only with one admin-granted, single-use approval
        bound to this task, engagement and the full ordered step list. Without one, an
        approval request is recorded and a failed result is returned before any step runs;
        the worker then parks the task in AWAITING_APPROVAL.
        """
        user_id = task.user_id
        plan = await self._stored_or_new_plan(task)

        if plan_requires_approval(plan):
            granted = self.approvals.authorize_or_request(
                task_id=task.id,
                agent_id=ORCHESTRATOR_AGENT_ID,
                engagement_id=user_id,
                operation=PLAN_APPROVAL_OPERATION,
                permission=PLAN_APPROVAL_PERMISSION,
                arguments=plan_approval_arguments(plan, task.priority),
                reason=f"Plan with {len(plan.steps)} step(s) requires approval (task priority {task.priority})",
            )
            self.audit.record(
                AuditEventType.APPROVAL_GRANTED if granted else AuditEventType.APPROVAL_REQUESTED,
                task_id=task.id,
                user_id=user_id,
                details={
                    "operation": PLAN_APPROVAL_OPERATION,
                    "step_count": len(plan.steps),
                    "result": "approval_consumed" if granted else "approval_required",
                },
            )
            if not granted:
                return AgentResult(
                    step_id=plan.steps[0].step_id,
                    agent_id=ORCHESTRATOR_AGENT_ID,
                    success=False,
                    error=f"Approval required for plan ({len(plan.steps)} step(s))",
                )

        return await self._run_steps(plan, task, user_id)

    async def _stored_or_new_plan(self, task: Task) -> Plan:
        """Return the task's stored plan, or plan it now and store the plan (first writer wins)."""
        record = self.task_tracker.get_task(task.id)
        if record is not None and record.plan is not None:
            logger.info("Reusing stored plan", task_id=str(task.id), step_count=len(record.plan.steps))
            return record.plan

        new_plan = await self.planner.plan(task)
        stored, created = self.task_tracker.set_plan_if_absent(task.id, new_plan)
        plan = stored or new_plan
        if created or stored is None:
            self.audit.record(
                AuditEventType.PLAN_CREATED,
                task_id=task.id,
                user_id=task.user_id,
                details=_plan_created_details(plan),
            )
        return plan

    async def _run_steps(self, plan: Plan, task: Task, user_id: Optional[str]) -> Optional[AgentResult]:
        """Run plan steps in order; the first failed step stops the plan.

        For multi-step plans a failed result's error names the step that failed.
        """
        total = len(plan.steps)
        previous: Dict[int, AgentResult] = {}
        result: Optional[AgentResult] = None
        for index, step in enumerate(plan.steps, start=1):
            result = await self._execute_step(step, task, plan, user_id, previous)
            if not result.success:
                if total > 1:
                    error = f"Plan step {index} of {total} failed: {result.error or 'unknown error'}"
                    result = result.model_copy(update={"error": error})
                return result
            previous[step.step_id] = result
        return result

    async def _execute_step(
        self,
        step: PlanStep,
        task: Task,
        plan: Plan,
        user_id: Optional[str] = None,
        previous_results: Optional[Dict[int, AgentResult]] = None,
    ) -> AgentResult:
        """Execute one planned step, returning a typed failure when unassigned."""
        del plan  # Reserved for dependency-aware execution in a later phase.
        agent_spec = await self.selection.select_agent(step, task)
        if agent_spec is None:
            logger.warning("No enabled agent available", task_id=str(task.id), step_id=step.step_id)
            return AgentResult(
                step_id=step.step_id,
                agent_id=step.agent_id or AgentId(id="unassigned"),
                success=False,
                error="No enabled agent available",
            )

        self.audit.record(
            AuditEventType.AGENT_SELECTED,
            task_id=task.id,
            agent_id=agent_spec.id,
            user_id=user_id,
            details={"step_id": step.step_id},
        )

        agent = self.agents.get(str(agent_spec.id))
        context = ExecutionContext(
            task_id=task.id,
            plan_step=step,
            task=task,
            agent=agent_spec,
            previous_results=dict(previous_results or {}),
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
