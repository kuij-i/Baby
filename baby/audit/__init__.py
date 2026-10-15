"""Audit logging system for BABY.

Provides an in-memory audit trail for compliance and debugging. The storage
implementation is intentionally replaceable: ``audit_log.use_backend(SQLiteAuditLog())``
routes every record and query of the process-wide ``audit_log`` to durable storage.
The API lifespan and every worker process attach the SQLite backend when
``audit_storage_type='sqlite'``, so events recorded by isolated worker processes are
visible through the API.
"""

import threading
from datetime import datetime
from typing import Any, Optional, Protocol
from uuid import UUID, uuid5

from baby.core import AgentId, AuditEvent, AuditEventType, TaskId

# Namespace for deterministic audit event IDs (events that must be recorded exactly once).
_AUDIT_EVENT_NAMESPACE = UUID("6f1d2c3e-9a4b-5c7d-8e0f-1a2b3c4d5e6f")


def task_created_event_id(task_id: TaskId) -> UUID:
    """Deterministic ID of a task's ``TASK_CREATED`` event, so it can be written at most once.

    Lets an idempotent retry re-attempt a creation audit event that failed to write
    without ever producing a second one.
    """
    return uuid5(_AUDIT_EVENT_NAMESPACE, f"task_created:{task_id}")


class AuditBackend(Protocol):
    """Storage interface shared by ``AuditLog`` and ``SQLiteAuditLog``."""

    def record(
        self,
        event_type: AuditEventType,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        user_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> AuditEvent: ...

    def record_once(
        self,
        event_id: UUID,
        event_type: AuditEventType,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        user_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> Optional[AuditEvent]: ...

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
    ) -> list[AuditEvent]: ...

    def count_events(
        self,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        event_type: Optional[AuditEventType] = None,
        user_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> int: ...

    def clear(self) -> None: ...


class AuditLog:
    """In-memory audit log for isolated tests and default in-process auditing."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._backend: Optional[AuditBackend] = None
        self._lock = threading.Lock()

    @property
    def backend(self) -> Optional[AuditBackend]:
        """Durable backend currently receiving events, or None for in-memory storage."""
        return self._backend

    def use_backend(self, backend: Optional[AuditBackend]) -> Optional[AuditBackend]:
        """Route records and queries to ``backend`` (None restores in-memory). Returns the previous backend."""
        previous = self._backend
        self._backend = backend
        return previous

    def record(
        self,
        event_type: AuditEventType,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        user_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> AuditEvent:
        """Record an audit event without storing secrets in event details."""
        if self._backend is not None:
            return self._backend.record(
                event_type, task_id=task_id, agent_id=agent_id, user_id=user_id, details=details
            )
        event = AuditEvent(
            event_type=event_type,
            task_id=task_id,
            agent_id=agent_id,
            user_id=user_id,
            details=details or {},
        )
        self._events.append(event)
        return event

    def record_once(
        self,
        event_id: UUID,
        event_type: AuditEventType,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        user_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> Optional[AuditEvent]:
        """Record an event with a fixed ``event_id`` unless one with that ID already exists.

        Returns the new event, or None if it was already recorded (nothing is written).
        Raises if the write fails, so callers can never mistake a failure for success.
        """
        if self._backend is not None:
            return self._backend.record_once(
                event_id, event_type, task_id=task_id, agent_id=agent_id, user_id=user_id, details=details
            )
        with self._lock:
            if any(event.id == event_id for event in self._events):
                return None
            event = AuditEvent(
                id=event_id,
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
        if self._backend is not None:
            return self._backend.get_events(
                task_id=task_id,
                agent_id=agent_id,
                event_type=event_type,
                user_id=user_id,
                start_time=start_time,
                end_time=end_time,
                limit=limit,
                offset=offset,
            )
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
        if self._backend is not None:
            return self._backend.count_events(
                task_id=task_id,
                agent_id=agent_id,
                event_type=event_type,
                user_id=user_id,
                start_time=start_time,
                end_time=end_time,
            )
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
        """Clear all audit events (and the attached backend's); intended for isolated tests."""
        self._events.clear()
        if self._backend is not None:
            self._backend.clear()


# Global in-memory audit log instance
audit_log = AuditLog()

__all__ = [
    "AuditBackend",
    "AuditLog",
    "audit_log",
    "task_created_event_id",
]
