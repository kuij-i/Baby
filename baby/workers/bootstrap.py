"""Worker process bootstrap: registers agents and their declared permissions.

A worker process starts with empty registries (it shares no memory with the API). The
bootstrap populates them from code, never from task data. The default bootstrap registers
only the CodingAgent with exactly its declared permissions (LOCAL_READ = ALLOW,
LOCAL_WRITE = APPROVAL_REQUIRED). No financial/trading, command execution, network or
secret permissions are granted, so any tool requiring them is denied by the executor.

A model provider is registered only when its credentials are configured (``OPENAI_API_KEY``);
without one the CodingAgent runs explicit tool steps and "list files" requests and fails
every other step explicitly instead of reporting it completed.

``CODING_REPO_ROOT`` is validated before the CodingAgent is registered: a missing, invalid
or unsafe root (one containing Baby's database or .env) raises ``ConfigurationError``, so
the worker refuses to start (config exit code, not restarted) instead of running tasks
with broken or over-broad coding tools.
"""

import importlib
from typing import Callable

from baby.errors import ConfigurationError

DEFAULT_BOOTSTRAP = "baby.workers.bootstrap:default_bootstrap"


def default_bootstrap() -> None:
    """Register the CodingAgent (bounded to a validated ``settings.coding_repo_root``) and its permissions."""
    from baby.agents import agent_registry
    from baby.agents.coding import CodingAgent, create_default_coding_spec
    from baby.permissions import permission_manager
    from baby.providers.registry import register_configured_providers
    from baby.tools.coding.path_resolver import validate_coding_repo_root

    repo_root = validate_coding_repo_root()
    register_configured_providers()
    spec = create_default_coding_spec()
    agent_id = str(spec.id)
    if agent_id not in {str(a.spec.id) for a in agent_registry.list_agents()}:
        agent_registry.register(CodingAgent(spec=spec, repo_root=str(repo_root)))
    for permission in spec.permissions:
        permission_manager.revoke_permission(agent_id, permission.category)
        permission_manager.grant_permission(agent_id, permission)


def resolve_bootstrap(path: str) -> Callable[[], None]:
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
