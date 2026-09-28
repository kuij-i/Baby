"""Health check system for BABY.

Inspects actual operational subsystems (memory, audit, agents, tools, providers)
to report genuine status: healthy, degraded, or unavailable.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict

from pydantic import BaseModel, Field

from baby.agents import agent_registry
from baby.audit import audit_log
from baby.logging import get_logger
from baby.memory import memory_store
from baby.providers import provider_registry
from baby.tools import tool_registry

logger = get_logger(__name__)


class HealthStatus(str, Enum):
    """Health check status states."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ComponentHealth(BaseModel):
    """Health status for a single subsystem."""

    status: HealthStatus
    message: str = "Operating normally"
    details: Dict[str, Any] = Field(default_factory=dict)


class SystemHealth(BaseModel):
    """Aggregated health status for the BABY platform."""

    status: HealthStatus
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    version: str = "0.1.0"
    components: Dict[str, ComponentHealth] = Field(default_factory=dict)


class HealthChecker:
    """Performs read-only health inspection of all BABY subsystems."""

    def check_memory(self) -> ComponentHealth:
        """Inspect memory subsystem."""
        try:
            # Attempt a bounded search to confirm the memory store responds
            records = memory_store.search(limit=1)
            return ComponentHealth(
                status=HealthStatus.HEALTHY,
                message="Memory store operational",
                details={"record_count_sample": len(records)},
            )
        except Exception as exc:
            logger.error("Memory health check failed", exc=exc)
            return ComponentHealth(
                status=HealthStatus.UNAVAILABLE,
                message="Memory store unavailable",
            )

    def check_audit(self) -> ComponentHealth:
        """Inspect audit subsystem."""
        try:
            count = audit_log.count_events() if hasattr(audit_log, "count_events") else len(audit_log.get_events())
            return ComponentHealth(
                status=HealthStatus.HEALTHY,
                message="Audit log operational",
                details={"event_count": count},
            )
        except Exception as exc:
            logger.error("Audit health check failed", exc=exc)
            return ComponentHealth(
                status=HealthStatus.UNAVAILABLE,
                message="Audit subsystem unavailable",
            )

    def check_agents(self) -> ComponentHealth:
        """Inspect agent registry and active agents."""
        try:
            agents = agent_registry.list_agents()
            enabled_agents = [a for a in agents if a.spec.enabled]
            if not agents:
                return ComponentHealth(
                    status=HealthStatus.DEGRADED,
                    message="No agents registered",
                    details={"registered_count": 0, "enabled_count": 0},
                )
            if not enabled_agents:
                return ComponentHealth(
                    status=HealthStatus.DEGRADED,
                    message="All registered agents are disabled",
                    details={"registered_count": len(agents), "enabled_count": 0},
                )
            return ComponentHealth(
                status=HealthStatus.HEALTHY,
                message=f"{len(enabled_agents)} agents active and enabled",
                details={"registered_count": len(agents), "enabled_count": len(enabled_agents)},
            )
        except Exception as exc:
            logger.error("Agent health check failed", exc=exc)
            return ComponentHealth(
                status=HealthStatus.DEGRADED,
                message="Agent registry check failed",
            )

    def check_tools(self) -> ComponentHealth:
        """Inspect tool registry."""
        try:
            tools = tool_registry.list_tools()
            return ComponentHealth(
                status=HealthStatus.HEALTHY,
                message=f"{len(tools)} tools registered",
                details={"tool_count": len(tools)},
            )
        except Exception as exc:
            logger.error("Tool health check failed", exc=exc)
            return ComponentHealth(
                status=HealthStatus.DEGRADED,
                message="Tool registry check failed",
            )

    def check_providers(self) -> ComponentHealth:
        """Inspect model provider status."""
        try:
            providers = provider_registry.list_providers()
            available = [p for p in providers if p.is_available()]
            if not providers:
                return ComponentHealth(
                    status=HealthStatus.DEGRADED,
                    message="No model providers registered",
                    details={"total_providers": 0, "available_providers": 0},
                )
            if not available:
                return ComponentHealth(
                    status=HealthStatus.DEGRADED,
                    message="No model providers currently available (API keys unconfigured or invalid)",
                    details={"total_providers": len(providers), "available_providers": 0},
                )
            return ComponentHealth(
                status=HealthStatus.HEALTHY,
                message=f"{len(available)} model provider(s) available",
                details={"total_providers": len(providers), "available_providers": len(available)},
            )
        except Exception as exc:
            logger.error("Provider health check failed", exc=exc)
            return ComponentHealth(
                status=HealthStatus.DEGRADED,
                message="Provider availability check failed",
            )

    def check_health(self) -> SystemHealth:
        """Perform comprehensive health check and aggregate state."""
        components = {
            "memory": self.check_memory(),
            "audit": self.check_audit(),
            "agents": self.check_agents(),
            "tools": self.check_tools(),
            "providers": self.check_providers(),
        }

        # Critical components determine UNAVAILABLE
        critical_components = ["memory", "audit"]
        is_unavailable = any(
            components[comp].status == HealthStatus.UNAVAILABLE for comp in critical_components if comp in components
        )

        if is_unavailable:
            overall_status = HealthStatus.UNAVAILABLE
        elif any(c.status in (HealthStatus.DEGRADED, HealthStatus.UNAVAILABLE) for c in components.values()):
            overall_status = HealthStatus.DEGRADED
        else:
            overall_status = HealthStatus.HEALTHY

        return SystemHealth(
            status=overall_status,
            components=components,
        )


# Global health checker instance
health_checker = HealthChecker()
