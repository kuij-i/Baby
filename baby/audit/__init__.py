"""Audit logging system for BABY.

Provides immutable audit trail for compliance and debugging.
"""

from datetime import datetime
from typing import Optional

from baby.core import AuditEvent, AuditEventType, TaskId, AgentId


class AuditLog:
    """In-memory audit log. Can be extended to use database."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record(
        self,
        event_type: AuditEventType,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        user_id: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> AuditEvent:
        """Record an audit event.

        Args:
            event_type: Type of event
            task_id: Associated task ID
            agent_id: Associated agent ID
            user_id: User who initiated action
            details: Additional event details

        Returns:
            Recorded audit event
        """
        event = AuditEvent(
            event_type=event_type,
            task_id=task_id,
            agent_id=agent_id,
            user_id=user_id,
            details=details or {},
        )
        self._events.append(event)
        return event

    def get_events(
        self,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        event_type: Optional[AuditEventType] = None,
    ) -> list[AuditEvent]:
        """Retrieve audit events.

        Args:
            task_id: Filter by task ID
            agent_id: Filter by agent ID
            event_type: Filter by event type

        Returns:
            List of matching audit events
        """
        events = self._events

        if task_id is not None:
            events = [e for e in events if e.task_id == task_id]

        if agent_id is not None:
            events = [e for e in events if e.agent_id == agent_id]

        if event_type is not None:
            events = [e for e in events if e.event_type == event_type]

        return events

    def clear(self) -> None:
        """Clear all audit events (for testing only)."""
        self._events.clear()


# Global audit log instance
audit_log = AuditLog()
