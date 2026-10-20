"""Runtime context: the set of services one Baby runtime uses, passed explicitly.

Most subsystems are still module-level singletons (``audit_log``, ``task_tracker``, ...). This
module is the first, non-breaking step away from them:

- ``BabyContext`` bundles the services a runtime needs.
- ``default_context()`` is a view over the existing module singletons (the same objects), so the
  API lifespan and worker processes can attach "their" context explicitly while every existing
  import keeps working.
- ``build_default_context(settings, database_url)`` builds a fresh, isolated set of services on its
  own SQLite database, so independent runtimes can coexist in one process (e.g. in tests) without
  ``patch()`` or global resets.

Components accept their dependencies as keyword-only constructor parameters that default to the
singletons, resolved when used (see ``Orchestrator``, ``ToolExecutor``, ``CodingAgent``,
``WorkerRuntime``).

Contexts are per process: a ``BabyContext`` refuses to be pickled, so it can never cross the worker
spawn boundary. Worker processes build their own from forwarded, non-secret settings
(``FORWARDABLE_SETTINGS``); secrets come from each process's environment.

Nothing in ``baby/`` imports this module at import time (only lazily, inside functions), so it
cannot introduce import cycles such as the planning <-> orchestration one.
"""

import importlib
from dataclasses import dataclass
from typing import Any, NoReturn, Optional

from baby.agents.registry import AgentRegistry
from baby.audit import AuditLog
from baby.configuration import Settings
from baby.memory.base import MemoryStore
from baby.observability.health import HealthChecker
from baby.observability.metrics import MetricsCollector
from baby.observability.tasks import TaskTracker
from baby.observability.workers import WorkerTracker
from baby.orchestration.orchestrator import Orchestrator
from baby.permissions.approvals import ApprovalService
from baby.permissions.manager import PermissionManager
from baby.persistence.database import DatabaseManager
from baby.planning.planner import TaskPlanner
from baby.planning.selection import SelectionLogic
from baby.providers.registry import ProviderRegistry
from baby.tools.executor import ToolExecutor
from baby.tools.registry import ToolRegistry


@dataclass(frozen=True)
class BabyContext:
    """The services one Baby runtime uses.

    ``metrics_collector`` and ``health_checker`` are still process-wide in every context (metric
    labels never contain task or worker IDs; the health checker reads the singletons).
    """

    settings: Settings
    db_manager: DatabaseManager
    audit_log: AuditLog
    permission_manager: PermissionManager
    approval_service: ApprovalService
    agent_registry: AgentRegistry
    tool_registry: ToolRegistry
    tool_executor: ToolExecutor
    provider_registry: ProviderRegistry
    task_planner: TaskPlanner
    selection_logic: SelectionLogic
    orchestrator: Orchestrator
    task_tracker: TaskTracker
    worker_tracker: WorkerTracker
    memory_store: MemoryStore
    metrics_collector: MetricsCollector
    health_checker: HealthChecker

    def __reduce__(self) -> NoReturn:
        raise TypeError(
            "BabyContext is per process and cannot be pickled; worker processes build their own context "
            "from FORWARDABLE_SETTINGS"
        )


def default_context() -> BabyContext:
    """Return the process's default context: the existing module-level singletons.

    The singletons are re-read on every call, so the result always reflects the current module
    attributes (including test patches); it holds no state of its own.
    """
    from baby.configuration import settings

    def singleton(module: str, name: str) -> Any:
        # Resolve the module object explicitly: some packages re-export an instance under the
        # submodule's own name (e.g. ``baby.orchestration.orchestrator``).
        return getattr(importlib.import_module(module), name)

    return BabyContext(
        settings=settings,
        db_manager=singleton("baby.persistence.database", "db_manager"),
        audit_log=singleton("baby.audit", "audit_log"),
        permission_manager=singleton("baby.permissions.manager", "permission_manager"),
        approval_service=singleton("baby.permissions.approvals", "approval_service"),
        agent_registry=singleton("baby.agents.registry", "agent_registry"),
        tool_registry=singleton("baby.tools.registry", "tool_registry"),
        tool_executor=singleton("baby.tools.executor", "tool_executor"),
        provider_registry=singleton("baby.providers.registry", "provider_registry"),
        task_planner=singleton("baby.planning.planner", "task_planner"),
        selection_logic=singleton("baby.planning.selection", "selection_logic"),
        orchestrator=singleton("baby.orchestration.orchestrator", "orchestrator"),
        task_tracker=singleton("baby.observability.tasks", "task_tracker"),
        worker_tracker=singleton("baby.observability.workers", "worker_tracker"),
        memory_store=singleton("baby.memory.in_memory", "memory_store"),
        metrics_collector=singleton("baby.observability.metrics", "metrics_collector"),
        health_checker=singleton("baby.observability.health", "health_checker"),
    )


def build_default_context(settings: Settings, database_url: Optional[str] = None) -> BabyContext:
    """Build a fresh, isolated context with its own database and services.

    Args:
        settings: Settings the context is built from (stored on the context). Components still read
            the process-wide ``settings`` for tuning values; that has not moved to the context yet.
        database_url: SQLite URL for this context; defaults to ``settings.database_url``.

    Returns:
        A context whose registries, permission manager, audit log, task tracker, approval service,
        tool executor, planner, selection logic and orchestrator are all new and wired to each other.

    Raises:
        ConfigurationError: The database cannot be opened or initialised (fail closed).
    """
    from baby.memory.in_memory import InMemoryStore
    from baby.observability.health import health_checker
    from baby.observability.metrics import metrics_collector
    from baby.persistence.sqlite_audit import SQLiteAuditLog
    from baby.persistence.sqlite_memory import SQLiteMemoryStore
    from baby.persistence.sqlite_tasks import SQLiteTaskStore
    from baby.planning.planner import ModelPlanner

    db = DatabaseManager(database_url or settings.database_url)
    db.init_schema()

    audit = AuditLog()
    if settings.audit_storage_type == "sqlite":
        audit.use_backend(SQLiteAuditLog(db))

    task_tracker = TaskTracker(task_store=SQLiteTaskStore(db))
    permissions = PermissionManager()
    agents = AgentRegistry()
    tools = ToolRegistry()
    providers = ProviderRegistry()
    approvals = ApprovalService(task_tracker=task_tracker)
    planner = TaskPlanner(model_planner=ModelPlanner(registry=providers))
    selection = SelectionLogic(agents=agents, tools=tools)
    memory: MemoryStore = SQLiteMemoryStore(db) if settings.memory_storage_type == "sqlite" else InMemoryStore()

    return BabyContext(
        settings=settings,
        db_manager=db,
        audit_log=audit,
        permission_manager=permissions,
        approval_service=approvals,
        agent_registry=agents,
        tool_registry=tools,
        tool_executor=ToolExecutor(permissions=permissions, audit=audit, approvals=approvals),
        provider_registry=providers,
        task_planner=planner,
        selection_logic=selection,
        orchestrator=Orchestrator(
            task_tracker=task_tracker,
            audit=audit,
            planner=planner,
            selection=selection,
            agents=agents,
            approvals=approvals,
        ),
        task_tracker=task_tracker,
        worker_tracker=WorkerTracker(),
        memory_store=memory,
        metrics_collector=metrics_collector,
        health_checker=health_checker,
    )


def close_context(context: BabyContext) -> None:
    """Close a context's database connections (for contexts built by ``build_default_context``)."""
    context.db_manager.close()
