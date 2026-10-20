"""Worker process bootstrap: registers agents and their declared permissions.

A worker process starts with empty registries (it shares no memory with the API). The
bootstrap populates them from code, never from task data. The default bootstrap registers
only the CodingAgent with exactly its declared permissions (LOCAL_READ = ALLOW,
LOCAL_WRITE = APPROVAL_REQUIRED). No financial/trading, command execution, network or
secret permissions are granted, so any tool requiring them is denied by the executor.

A model provider is registered only when its credentials are configured (``NVIDIA_API_KEY`` or
``OPENAI_API_KEY``, chosen by ``MODEL_PROVIDER``);
without one the CodingAgent runs explicit tool steps and "list files" requests and fails
every other step explicitly instead of reporting it completed.

``CODING_REPO_ROOT`` is validated before the CodingAgent is registered: a missing, invalid
or unsafe root (one containing Baby's database or .env) raises ``ConfigurationError``, so
the worker refuses to start (config exit code, not restarted) instead of running tasks
with broken or over-broad coding tools.
"""

import importlib
import inspect
from typing import TYPE_CHECKING, Callable, List, Optional

from baby.errors import ConfigurationError

if TYPE_CHECKING:
    from baby.core import AgentSpec
    from baby.runtime_context import BabyContext

DEFAULT_BOOTSTRAP = "baby.workers.bootstrap:default_bootstrap"


def default_bootstrap(context: "Optional[BabyContext]" = None) -> None:
    """Register the CodingAgent (bounded to a validated ``settings.coding_repo_root``) and its permissions.

    Args:
        context: Context whose registries, permission manager, tool executor and audit log the agent is
            registered with and wired to; defaults to the process's default context (the singletons).
    """
    from baby.agents.coding import CodingAgent, create_default_coding_spec
    from baby.providers.registry import register_configured_providers
    from baby.runtime_context import default_context
    from baby.tools.coding.path_resolver import validate_coding_repo_root

    ctx = context if context is not None else default_context()
    repo_root = validate_coding_repo_root()
    register_configured_providers(ctx.provider_registry)
    spec = create_default_coding_spec()
    agent_id = str(spec.id)
    if agent_id not in {str(a.spec.id) for a in ctx.agent_registry.list_agents()}:
        ctx.agent_registry.register(
            CodingAgent(
                spec=spec,
                repo_root=str(repo_root),
                tool_executor=ctx.tool_executor,
                audit=ctx.audit_log,
                providers=ctx.provider_registry,
            )
        )
    for permission in spec.permissions:
        ctx.permission_manager.revoke_permission(agent_id, permission.category)
        ctx.permission_manager.grant_permission(agent_id, permission)


def default_agent_specs() -> "List[AgentSpec]":
    """Specs of the agents ``default_bootstrap`` registers, for read-only visibility outside workers."""
    from baby.agents.coding import create_default_coding_spec

    return [create_default_coding_spec()]


def run_bootstrap(bootstrap: Callable[..., None], context: "BabyContext") -> None:
    """Call a bootstrap with ``context`` if it accepts an argument, else with none (older bootstraps)."""
    try:
        accepts_context = bool(inspect.signature(bootstrap).parameters)
    except (TypeError, ValueError):
        accepts_context = False
    if accepts_context:
        bootstrap(context)
    else:
        bootstrap()


def resolve_bootstrap(path: str) -> Callable[..., None]:
    """Resolve a ``"module:function"`` bootstrap reference (configured by the operator, never by tasks)."""
    module_name, _, attr = path.partition(":")
    if not module_name or not attr:
        raise ConfigurationError(f"Invalid worker bootstrap reference '{path}'; expected 'module:function'")
    try:
        func = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise ConfigurationError(f"Cannot load worker bootstrap '{path}': {exc}") from exc
    if not callable(func):
        raise ConfigurationError(f"Worker bootstrap '{path}' is not callable")
    return func  # type: ignore[no-any-return]
