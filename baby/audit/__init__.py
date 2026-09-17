"""Audit logging system for BABY.

Provides an in-memory audit trail for compliance and debugging. The storage
implementation is intentionally replaceable so it can later be backed by
SQLite or another durable store.
"""

from typing import Any, Optional

from baby.core import AgentId, AuditEvent, AuditEventType, TaskId


class AuditLog:
    """In-memory audit log."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record(
        self,
        event_type: AuditEventType,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        user_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> AuditEvent:
        """Record an audit event without storing secrets in event details."""
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
        """Retrieve events filtered by task, agent, and/or event type."""
        events = self._events
        if task_id is not None:
            events = [event for event in events if event.task_id == task_id]
        if agent_id is not None:
            events = [event for event in events if event.agent_id == agent_id]
        if event_type is not None:
            events = [event for event in events if event.event_type == event_type]
        return list(events)

    def get_events_by_task(self, task_id: TaskId) -> list[AuditEvent]:
        """Compatibility convenience method for retrieving task events."""
        return self.get_events(task_id=task_id)

    def clear(self) -> None:
        """Clear all audit events; intended for isolated tests."""
        self._events.clear()


# Global audit log instance
audit_log = AuditLog()
