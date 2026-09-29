"""Audit logging system for BABY.

Provides an in-memory audit trail for compliance and debugging. The storage
implementation is intentionally replaceable so it can be backed by
SQLite or another durable store.
"""

from datetime import datetime
from typing import Any, Optional

from baby.core import AgentId, AuditEvent, AuditEventType, TaskId


class AuditLog:
    """In-memory audit log for isolated tests and default in-process auditing."""

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

    def _filter_events(
        self,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        event_type: Optional[AuditEventType] = None,
        user_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> list[AuditEvent]:
        events = self._events
        if task_id is not None:
            events = [event for event in events if event.task_id == task_id]
        if agent_id is not None:
            events = [event for event in events if event.agent_id == agent_id]
        if event_type is not None:
            events = [event for event in events if event.event_type == event_type]
        if user_id is not None:
            events = [event for event in events if event.user_id == user_id]
        if start_time is not None:
            events = [event for event in events if event.timestamp >= start_time]
        if end_time is not None:
            events = [event for event in events if event.timestamp <= end_time]
        return list(events)

    def get_events(
        self,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        event_type: Optional[AuditEventType] = None,
        user_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[AuditEvent]:
        """Retrieve events filtered by task, agent, event type, user/engagement, and/or time range with pagination."""
        matched = self._filter_events(
            task_id=task_id,
            agent_id=agent_id,
            event_type=event_type,
            user_id=user_id,
            start_time=start_time,
            end_time=end_time,
        )
        if offset > 0:
            matched = matched[offset:]
        if limit is not None and limit >= 0:
            matched = matched[:limit]
        return matched

    def count_events(
        self,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        event_type: Optional[AuditEventType] = None,
        user_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> int:
        """Count events matching the specified filters."""
        return len(
            self._filter_events(
                task_id=task_id,
                agent_id=agent_id,
                event_type=event_type,
                user_id=user_id,
                start_time=start_time,
                end_time=end_time,
            )
        )

    def get_events_by_task(self, task_id: TaskId) -> list[AuditEvent]:
        """Compatibility convenience method for retrieving task events."""
        return self.get_events(task_id=task_id)

    def clear(self) -> None:
        """Clear all audit events; intended for isolated tests."""
        self._events.clear()


# Global in-memory audit log instance
audit_log = AuditLog()

__all__ = [
    "AuditLog",
    "audit_log",
]
