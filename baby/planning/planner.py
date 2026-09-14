"""Task planner for decomposing tasks into executable steps."""

from typing import List
from baby.core import Task, Plan, PlanStep, AgentId
from baby.logging import get_logger

logger = get_logger(__name__)


class TaskPlanner:
    """Decomposes tasks into executable plans.
    
    The planner takes a user task and breaks it into concrete steps
    that can be assigned to agents and tools.
    """

    async def plan(self, task: Task) -> Plan:
        """Create a plan for a task.
        
        Args:
            task: Task to plan
            
        Returns:
            Plan with executable steps
        """
        logger.info(f"Planning task: {task.title}")
        
        # For MVP, create simple single-step plan
        # In production, this would use LLM to decompose complex tasks
        steps = self._decompose_task(task)
        
        plan = Plan(
            task_id=task.id,
            steps=steps,
            reasoning="Task decomposed into executable steps",
        )
        
        logger.info(
            f"Plan created with {len(steps)} steps",
            task_id=str(task.id),
        )
        
        return plan

    def _decompose_task(self, task: Task) -> List[PlanStep]:
        """Decompose a task into steps.
        
        Args:
            task: Task to decompose
            
        Returns:
            List of plan steps
        """
        # MVP: Single step to execute task
        # Production: Use LLM to create multi-step plans
        return [
            PlanStep(
                step_id=1,
                description=task.description,
                approval_required=task.priority >= 8,  # High priority needs approval
            )
        ]


# Global planner instance
task_planner = TaskPlanner()
