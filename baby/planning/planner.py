"""Task planner for decomposing tasks into executable steps."""

from typing import Any, List

from baby.agents import CODING_AGENT_ID
from baby.core import AgentId, Plan, PlanStep, Task
from baby.logging import get_logger

logger = get_logger(__name__)

CODING_OPERATIONS = {
    "list_files": "repository_list_files",
    "read_file": "repository_read_file",
    "write_file": "repository_write_file",
}


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
        if self._is_coding_task(task):
            operation = str(task.metadata.get("operation", "list_files"))
            tool_name = CODING_OPERATIONS.get(operation)
            tool_input = self._extract_tool_input(task.metadata)
            return [
                PlanStep(
                    step_id=1,
                    description=task.description,
                    agent_id=AgentId(id=CODING_AGENT_ID),
                    tool_name=tool_name,
                    approval_required=task.priority >= 8 or operation == "write_file",
                    metadata={
                        "domain": "coding",
                        "operation": operation,
                        "tool_input": tool_input,
                    },
                )
            ]

        return [
            PlanStep(
                step_id=1,
                description=task.description,
                approval_required=task.priority >= 8,
            )
        ]

    @staticmethod
    def _is_coding_task(task: Task) -> bool:
        if task.metadata.get("domain") == "coding":
            return True

        haystack = f"{task.title} {task.description}".lower()
        return any(keyword in haystack for keyword in ("code", "file", "bug", "refactor", "implement"))

    @staticmethod
    def _extract_tool_input(metadata: dict[str, Any]) -> dict[str, Any]:
        tool_input = metadata.get("tool_input")
        if isinstance(tool_input, dict):
            return dict(tool_input)

        extracted: dict[str, Any] = {}
        for key in ("path", "content"):
            value = metadata.get(key)
            if isinstance(value, str):
                extracted[key] = value
        return extracted


# Global planner instance
task_planner = TaskPlanner()
