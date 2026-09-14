"""Tests for audit logging."""

import pytest

from baby.audit import audit_log
from baby.core import AgentId, AuditEventType, TaskId


class TestAuditLog:
    """Tests for AuditLog."""

    def setup_method(self) -> None:
        """Clear audit log before each test."""
        audit_log.clear()

    def test_record_event(self) -> None:
        """Test recording an audit event."""
        task_id = TaskId()
        event = audit_log.record(
            event_type=AuditEventType.TASK_CREATED,
            task_id=task_id,
            user_id="user-1",
        )

        assert event.event_type == AuditEventType.TASK_CREATED
        assert event.task_id == task_id
        assert event.user_id == "user-1"

    def test_get_events_by_task(self) -> None:
        """Test retrieving events by task ID."""
        task_id1 = TaskId()
        task_id2 = TaskId()

        audit_log.record(AuditEventType.TASK_CREATED, task_id=task_id1)
        audit_log.record(AuditEventType.PLAN_CREATED, task_id=task_id1)
        audit_log.record(AuditEventType.TASK_CREATED, task_id=task_id2)

        events = audit_log.get_events(task_id=task_id1)
        assert len(events) == 2
        assert all(e.task_id == task_id1 for e in events)

    def test_get_events_by_event_type(self) -> None:
        """Test retrieving events by type."""
        task_id = TaskId()
        audit_log.record(AuditEventType.TASK_CREATED, task_id=task_id)
        audit_log.record(AuditEventType.PLAN_CREATED, task_id=task_id)
        audit_log.record(AuditEventType.TASK_CREATED, task_id=task_id)

        events = audit_log.get_events(event_type=AuditEventType.TASK_CREATED)
        assert len(events) == 2

    def test_get_events_by_agent(self) -> None:
        """Test retrieving events by agent ID."""
        agent_id1 = AgentId(id="agent-1")
        agent_id2 = AgentId(id="agent-2")

        audit_log.record(AuditEventType.AGENT_SELECTED, agent_id=agent_id1)
        audit_log.record(AuditEventType.AGENT_SELECTED, agent_id=agent_id2)
        audit_log.record(AuditEventType.AGENT_SELECTED, agent_id=agent_id1)

        events = audit_log.get_events(agent_id=agent_id1)
        assert len(events) == 2

    def test_multiple_filters(self) -> None:
        """Test retrieving events with multiple filters."""
        task_id1 = TaskId()
        task_id2 = TaskId()
        agent_id = AgentId(id="agent-1")

        audit_log.record(
            AuditEventType.AGENT_SELECTED, task_id=task_id1, agent_id=agent_id
        )
        audit_log.record(
            AuditEventType.PERMISSION_CHECKED, task_id=task_id1, agent_id=agent_id
        )
        audit_log.record(
            AuditEventType.AGENT_SELECTED, task_id=task_id2, agent_id=agent_id
        )

        events = audit_log.get_events(
            task_id=task_id1, event_type=AuditEventType.AGENT_SELECTED
        )
        assert len(events) == 1

    def test_event_with_details(self) -> None:
        """Test recording event with details."""
        task_id = TaskId()
        details = {"agent_name": "coding-agent", "capability": "code-review"}
        event = audit_log.record(
            AuditEventType.AGENT_SELECTED, task_id=task_id, details=details
        )

        assert event.details == details
