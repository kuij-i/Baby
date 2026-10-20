"""Task planners that decompose tasks into executable steps.

``TaskPlanner`` (the ``task_planner`` singleton) is the interface the orchestrator calls. It
delegates to a strategy selected by ``settings.planner_mode``:

- ``rule`` (default): ``RuleBasedPlanner``, deterministic, one step carrying the task description.
- ``model``: ``ModelPlanner``, the configured model provider decomposes the task into at most
  ``settings.planner_max_steps`` steps returned as strict JSON. Malformed output, too many steps
  or a provider error raise ``PlanningError`` (the task fails closed); there is no fallback plan.

A plan is created once per task and stored; resumed tasks reuse the stored plan (see the
orchestrator), so a non-deterministic planner can never change a task's plan or its approval digest.
"""

import json
from typing import TYPE_CHECKING, Any, List, Optional

from baby.core import Plan, PlanStep, Task
from baby.errors import PlanningError
from baby.logging import get_logger
from baby.observability.redaction import redact_sensitive_data
from baby.providers.base import ModelProvider
from baby.providers.types import ChatMessage

if TYPE_CHECKING:
    from baby.providers.registry import ProviderRegistry

logger = get_logger(__name__)

# Tasks at or above this priority need one admin approval for the whole plan before any step runs.
APPROVAL_PRIORITY = 8
MAX_STEP_DESCRIPTION_CHARS = 2000

MODEL_PLANNER_SYSTEM_PROMPT = (
    "You are the planner of a governed coding assistant. Decompose the user's task into an ordered list of "
    "small, concrete steps that a repository-bounded coding agent can carry out one at a time. Use at most "
    "{max_steps} steps; use a single step when the task is simple. Respond with exactly one JSON object and "
    'nothing else, of the form {{"steps": [{{"description": "<what to do in this step>"}}]}}.'
)


def _requires_approval(task: Task) -> bool:
    return task.priority >= APPROVAL_PRIORITY


class RuleBasedPlanner:
    """Deterministic planner: one step that carries the task description."""

    mode = "rule"

    async def plan(self, task: Task) -> Plan:
        """Create a single-step plan for a task.

        Args:
            task: Task to plan.

        Returns:
            Plan with one step; high-priority tasks require plan approval.
        """
        return Plan(
            task_id=task.id,
            steps=[PlanStep(step_id=1, description=task.description)],
            reasoning="Task executed as a single step",
            approval_required=_requires_approval(task),
            planner_mode=self.mode,
        )


class ModelPlanner:
    """Planner that asks the configured model provider to decompose a task (fails closed)."""

    mode = "model"

    def __init__(
        self,
        provider: Optional[ModelProvider] = None,
        max_steps: Optional[int] = None,
        registry: Optional["ProviderRegistry"] = None,
    ) -> None:
        """Initialize the model planner.

        Args:
            provider: Provider to use; defaults to the registry's default provider at plan time.
            max_steps: Maximum number of steps; defaults to ``settings.planner_max_steps``.
            registry: Provider registry to take the default provider from; defaults to ``provider_registry``.
        """
        self._provider = provider
        self._max_steps = max_steps
        self._registry = registry

    @property
    def max_steps(self) -> int:
        """Maximum number of steps a plan may contain."""
        if self._max_steps is not None:
            return self._max_steps
        from baby.configuration import settings

        return int(settings.planner_max_steps)

    def _get_provider(self) -> ModelProvider:
        if self._provider is not None:
            provider = self._provider
        else:
            from baby.providers.registry import provider_registry

            registry = self._registry if self._registry is not None else provider_registry
            try:
                provider = registry.get_default()
            except Exception as exc:
                raise PlanningError("planner_mode='model' but no model provider is configured") from exc
        if not provider.is_available():
            raise PlanningError(f"Model provider '{provider.name}' is not available for planning")
        return provider

    async def plan(self, task: Task) -> Plan:
        """Ask the model provider for a plan and validate it strictly.

        Args:
            task: Task to plan.

        Returns:
            Validated plan with 1..max_steps steps.

        Raises:
            PlanningError: No provider, provider failure, or output that is not a valid plan.
        """
        from baby.providers.registry import configured_model

        provider = self._get_provider()
        max_steps = self.max_steps
        messages = [
            ChatMessage(role="system", content=MODEL_PLANNER_SYSTEM_PROMPT.format(max_steps=max_steps)),
            ChatMessage(role="user", content=f"Task title: {task.title}\nTask description: {task.description}"),
        ]
        try:
            response = await provider.complete(messages, model=configured_model(), temperature=0.0)
        except Exception as exc:
            raise PlanningError(f"Model planner failed: {redact_sensitive_data(str(exc))}") from exc

        descriptions = parse_model_plan(response.content or "", max_steps)
        return Plan(
            task_id=task.id,
            steps=[PlanStep(step_id=i, description=d) for i, d in enumerate(descriptions, start=1)],
            reasoning=f"Task decomposed into {len(descriptions)} step(s) by the model planner",
            approval_required=_requires_approval(task),
            planner_mode=self.mode,
        )


def _strip_code_fence(text: str) -> str:
    """Remove one surrounding Markdown code fence (```json ... ```), if present."""
    if not text.startswith("```"):
        return text
    first_newline = text.find("\n")
    if first_newline == -1 or not text.endswith("```"):
        raise PlanningError("Model planner returned an unterminated code fence")
    if text[3:first_newline].strip().lower() not in ("", "json"):
        raise PlanningError("Model planner returned a non-JSON code block")
    return text[first_newline + 1 : -3].strip()


def parse_model_plan(content: str, max_steps: int) -> List[str]:
    """Validate a model planner response and return the ordered step descriptions.

    The response must be exactly one JSON object ``{"steps": [{"description": "..."}]}`` (optionally
    wrapped in a single ```json fence) with 1..max_steps steps and no other keys.

    Args:
        content: Raw model output.
        max_steps: Maximum number of steps allowed.

    Returns:
        Step descriptions in order.

    Raises:
        PlanningError: The output is not a valid plan.
    """
    text = _strip_code_fence(content.strip())
    try:
        parsed: Any = json.loads(text)
    except ValueError as exc:
        raise PlanningError("Model planner returned malformed JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"steps"}:
        raise PlanningError('Model planner output must be a JSON object with exactly one key, "steps"')
    steps = parsed["steps"]
    if not isinstance(steps, list) or not steps:
        raise PlanningError('Model planner "steps" must be a non-empty list')
    if len(steps) > max_steps:
        raise PlanningError(f"Model planner returned {len(steps)} steps, more than planner_max_steps ({max_steps})")
    descriptions: List[str] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or set(step) != {"description"}:
            raise PlanningError(f'Model planner step {index} must be an object with exactly one key, "description"')
        description = step["description"]
        if not isinstance(description, str) or not description.strip():
            raise PlanningError(f"Model planner step {index} has an empty description")
        if len(description) > MAX_STEP_DESCRIPTION_CHARS:
            raise PlanningError(f"Model planner step {index} description exceeds {MAX_STEP_DESCRIPTION_CHARS} chars")
        descriptions.append(description.strip())
    return descriptions


class TaskPlanner:
    """Planner interface used by the orchestrator; delegates to the strategy for ``settings.planner_mode``."""

    def __init__(self, rule_planner: Optional[RuleBasedPlanner] = None, model_planner: Optional[ModelPlanner] = None):
        """Initialize with optional strategy instances (defaults are created lazily per call).

        Args:
            rule_planner: Deterministic planner to use in ``rule`` mode.
            model_planner: Model planner to use in ``model`` mode.
        """
        self._rule_planner = rule_planner or RuleBasedPlanner()
        self._model_planner = model_planner

    async def plan(self, task: Task) -> Plan:
        """Create a plan for a task.

        Args:
            task: Task to plan.

        Returns:
            Plan with executable steps.

        Raises:
            PlanningError: The configured planner could not produce a valid plan.
        """
        from baby.configuration import settings

        mode = (settings.planner_mode or "rule").strip().lower()
        if mode == "model":
            planner: Any = self._model_planner or ModelPlanner()
        elif mode == "rule":
            planner = self._rule_planner
        else:
            raise PlanningError(f"Unknown planner_mode {settings.planner_mode!r}")
        plan: Plan = await planner.plan(task)
        logger.info("Plan created", task_id=str(task.id), step_count=len(plan.steps), planner_mode=plan.planner_mode)
        return plan


# Global planner instance
task_planner = TaskPlanner()
