"""Read-only agent visibility endpoints."""

from typing import Any, Dict, List, cast

from fastapi import APIRouter, Depends, HTTPException, status

from baby.agents import agent_registry
from baby.api.auth import AuthenticatedCaller, get_current_caller
from baby.observability.redaction import redact_sensitive_data

router = APIRouter(tags=["Agents"])


def _agent_to_view(agent: Any) -> Dict[str, Any]:
    """Convert an Agent instance to a safe operational representation."""
    spec = agent.spec
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


@router.get("/agents", response_model=List[Dict[str, Any]])
def list_agents(
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> List[Dict[str, Any]]:
    """Expose safe read-only visibility into registered agents."""
    agents = agent_registry.list_agents()
    views = [_agent_to_view(a) for a in agents]
    return cast(List[Dict[str, Any]], redact_sensitive_data(views))


@router.get("/agents/{agent_id}", response_model=Dict[str, Any])
def get_agent(
    agent_id: str,
    caller: AuthenticatedCaller = Depends(get_current_caller),
) -> Dict[str, Any]:
    """Retrieve safe operational visibility into a specific agent."""
    try:
        agent = agent_registry.get(agent_id)
        return cast(Dict[str, Any], redact_sensitive_data(_agent_to_view(agent)))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{agent_id}' not found",
        )
