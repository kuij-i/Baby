"""Importable helpers for durable worker tests.

Worker processes use the ``spawn`` start method, so anything a child process runs
(bootstraps, race participants) must live in an importable module, not in a test body.
"""

import asyncio
import json
from typing import Any, Dict, List

from baby.agents.base import Agent
from baby.core import AgentCapability, AgentId, AgentResult, AgentSpec, ExecutionContext
from baby.core.contracts import utc_now
from baby.providers.base import ModelProvider
from baby.providers.types import ProviderResponse, TokenUsage


class ScriptedAgent(Agent):
    """Test agent whose behaviour is driven by the task description.

    - ``sleep:<seconds>``  → await asyncio.sleep(seconds) then succeed
    - ``fail``             → return a failed result
    - ``raise``            → raise an exception
    - anything else        → succeed immediately
    """

    def __init__(self) -> None:
        super().__init__(
            AgentSpec(
                id=AgentId(id="scripted-agent"),
                name="ScriptedAgent",
                description="Deterministic test agent",
                role="testing",
                capabilities=[AgentCapability(name="test", description="test")],
            )
        )

    async def execute(self, context: ExecutionContext) -> AgentResult:
        description = context.task.description
        if description.startswith("sleep:"):
            await asyncio.sleep(float(description.split(":", 1)[1]))
        if description == "fail":
            return AgentResult(step_id=context.plan_step.step_id, agent_id=self.spec.id, success=False, error="boom")
        if description == "raise":
            raise RuntimeError("agent exploded")
        return AgentResult(
            step_id=context.plan_step.step_id,
            agent_id=self.spec.id,
            success=True,
            output={"echo": description},
        )


def bootstrap_scripted_agent() -> None:
    from baby.agents import agent_registry

    if "scripted-agent" not in {str(a.spec.id) for a in agent_registry.list_agents()}:
        agent_registry.register(ScriptedAgent())


class WriteFileProvider(ModelProvider):
    """Stateless scripted model: requests one write_file call, then finishes.

    Stateless so a resumed execution (after approval) proposes the identical operation.
    """

    def __init__(self, path: str = "approved.py", content: str = "x = 1\n") -> None:
        self.path = path
        self.content = content
        self.calls = 0

    @property
    def name(self) -> str:
        return "scripted-write"

    @property
    def supported_models(self) -> List[str]:
        return ["gpt-4"]

    def is_available(self) -> bool:
        return True

    async def complete(self, messages: Any, **kwargs: Any) -> ProviderResponse:
        return ProviderResponse(content="done", model="gpt-4")

    async def complete_with_tools(self, messages: Any, tools: Any, **kwargs: Any) -> ProviderResponse:
        self.calls += 1
        usage = TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        if messages and messages[-1].role == "tool":
            return ProviderResponse(content="written", model="gpt-4", tool_calls=None, usage=usage)
        call: Dict[str, Any] = {
            "id": f"call_{self.calls}",
            "type": "function",
            "function": {"name": "write_file", "arguments": json.dumps({"path": self.path, "content": self.content})},
        }
        return ProviderResponse(content="writing", model="gpt-4", tool_calls=[call], usage=usage)


def race_claim(database_url: str, worker_id: str, barrier: Any, results: Any, attempts: int) -> None:
    """Child-process claim racer: register, wait at the barrier, then claim as fast as possible."""

    from baby.core import WorkerStatus
    from baby.persistence.database import DatabaseManager
    from baby.persistence.sqlite_tasks import SQLiteTaskStore
    from baby.persistence.sqlite_workers import SQLiteWorkerStore
    from baby.workers.queue import DurableTaskQueue

    db = DatabaseManager(database_url)
    tasks = SQLiteTaskStore(db)
    workers = SQLiteWorkerStore(db)
    workers.register(worker_id, hostname="test", pid=0, now=utc_now())
    queue = DurableTaskQueue(tasks, workers, lease_duration_seconds=300)
    claimed: List[str] = []
    barrier.wait()
    for _ in range(attempts):
        got = queue.claim_next(worker_id)
        if got is None:
            break
        claimed.append(str(got.record.task_id))
        # Release the worker back to IDLE so it may claim again (lease stays: task is owned).
        workers.set_status(worker_id, WorkerStatus.IDLE, utc_now())
    results.put((worker_id, claimed))
    db.close()


def idempotent_submit_racer(database_url: str, key: str, barrier: Any, results: Any) -> None:
    """Child-process racer: submit the same task under the same Idempotency-Key as fast as possible."""
    from baby.core import Task
    from baby.persistence.database import DatabaseManager
    from baby.persistence.sqlite_tasks import SQLiteTaskStore
    from baby.persistence.sqlite_workers import SQLiteWorkerStore
    from baby.workers.queue import DurableTaskQueue, IdempotencyScope

    db = DatabaseManager(database_url)
    tasks = SQLiteTaskStore(db)
    queue = DurableTaskQueue(tasks, SQLiteWorkerStore(db))
    task = Task(title="race", description="same request", priority=1, user_id="eng-race")
    barrier.wait()
    record, replayed = queue.submit_idempotent(task, IdempotencyScope("operator-user", "eng-race", key))
    results.put((str(record.task_id), replayed))
    db.close()


def import_modules_in_order(module_names: List[str], results: Any) -> None:
    """Child-process importer: import modules in the given order in a fresh interpreter.

    Puts ``None`` on success, or the error text, so a parent test can detect import cycles that
    only show up for a particular first import.
    """
    import importlib

    try:
        for name in module_names:
            importlib.import_module(name)
    except Exception as exc:  # report, the parent asserts
        results.put(f"{type(exc).__name__}: {exc}")
        return
    results.put(None)
