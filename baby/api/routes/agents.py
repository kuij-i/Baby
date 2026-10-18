"""Read-only agent visibility endpoints."""

from typing import Any, Dict, List, Set, cast

from fastapi import APIRouter, Depends, HTTPException, status

from baby.agents import agent_registry
from baby.api.auth import AuthenticatedCaller, get_current_caller
from baby.api.schemas import AgentView
from baby.observability.redaction import redact_sensitive_data

router = APIRouter(tags=["Agents"])


def _worker_hosted_views() -> List[Dict[str, Any]]:
    """Views of the agents that live durable worker processes report hosting.

    Workers record the IDs their bootstrap registered; only agents with a spec known to the
    default bootstrap can be described (others are omitted rather than guessed).
    """
    from baby.observability.health import live_durable_workers
    from baby.workers.bootstrap import default_agent_specs

    hosted: Set[str] = set()
    for worker in live_durable_workers():
        hosted.update(a for a in str(worker.metadata.get("agents", "")).split(",") if a)
    return [_spec_to_view(spec) for spec in default_agent_specs() if str(spec.id) in hosted]


def _agent_to_view(agent: Any) -> Dict[str, Any]:
    """Convert an Agent instance to a safe operational representation."""
    return _spec_to_view(agent.spec)


def _spec_to_view(spec: Any) -> Dict[str, Any]:
    return {
        "id": str(spec.id),
        "name": spec.name,
        "description": spec.description,
        "role": spec.role,
        "enabled": spec.enabled,
        "model": spec.model,
        "context_window": spec.context_window,
        "max_budget": spec.max_budget,
        "capabilities": [c.model_dump() for c in spec.capabilities],
        "permissions": [p.model_dump() for p in spec.permissions],
    }


@router.get("/agents", response_model=List[AgentView])
def list_agents(
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> List[Dict[str, Any]]:
    """Expose safe read-only visibility into registered agents.

    Includes agents registered in this process and, because tasks run in worker processes,
    the agents hosted by live durable workers.
    """
    views = [_agent_to_view(a) for a in agent_registry.list_agents()]
    known = {view["id"] for view in views}
    views.extend(view for view in _worker_hosted_views() if view["id"] not in known)
    return cast(List[Dict[str, Any]], redact_sensitive_data(views))


@router.get("/agents/{agent_id}", response_model=AgentView)
def get_agent(
    agent_id: str,
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> Dict[str, Any]:
    """Retrieve safe operational visibility into a specific agent."""
    try:
        agent = agent_registry.get(agent_id)
        return cast(Dict[str, Any], redact_sensitive_data(_agent_to_view(agent)))
    except Exception:
        for view in _worker_hosted_views():
            if view["id"] == agent_id:
                return cast(Dict[str, Any], redact_sensitive_data(view))
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{agent_id}' not found",
        )
