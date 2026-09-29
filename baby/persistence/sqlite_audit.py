"""Durable SQLite implementation of the audit log."""

import json
import sqlite3
from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

from baby.core import AgentId, AuditEvent, AuditEventType, TaskId
from baby.persistence.database import DatabaseManager, db_manager


class SQLiteAuditLog:
    """Durable SQLite-backed audit log.

    Preserves audit history across application restarts with transactional guarantees,
    indexing on task_id, agent_id, user_id, event_type, and timestamp.
    """

    def __init__(self, manager: Optional[DatabaseManager] = None) -> None:
        self._db = manager or db_manager
        self._db.init_schema()

    def record(
        self,
        event_type: AuditEventType,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        user_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> AuditEvent:
        """Record an audit event durably in SQLite."""
        event = AuditEvent(
            id=uuid4(),
            event_type=event_type,
            task_id=task_id,
            agent_id=agent_id,
            user_id=user_id,
            timestamp=datetime.utcnow(),
            details=details or {},
        )

        task_id_str = str(task_id) if task_id else None
        agent_id_str = str(agent_id) if agent_id else None
        details_json = json.dumps(event.details)
        timestamp_str = event.timestamp.isoformat()

        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO audit_events (id, event_type, task_id, agent_id, user_id, timestamp, details)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    str(event.id),
                    str(event.event_type),
                    task_id_str,
                    agent_id_str,
                    user_id,
                    timestamp_str,
                    details_json,
                ),
            )

        return event

    def _build_query(
        self,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        event_type: Optional[AuditEventType] = None,
        user_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> tuple[str, list[Any]]:
        where_clauses = ["1=1"]
        params: list[Any] = []

        if task_id is not None:
            where_clauses.append("task_id = ?")
            params.append(str(task_id))

        if agent_id is not None:
            where_clauses.append("agent_id = ?")
            params.append(str(agent_id))

        if event_type is not None:
            where_clauses.append("event_type = ?")
            params.append(str(event_type.value if hasattr(event_type, "value") else event_type))

        if user_id is not None:
            where_clauses.append("user_id = ?")
            params.append(user_id)

        if start_time is not None:
            where_clauses.append("timestamp >= ?")
            params.append(start_time.isoformat())

        if end_time is not None:
            where_clauses.append("timestamp <= ?")
            params.append(end_time.isoformat())

        return " AND ".join(where_clauses), params

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
        where_sql, params = self._build_query(
            task_id=task_id,
            agent_id=agent_id,
            event_type=event_type,
            user_id=user_id,
            start_time=start_time,
            end_time=end_time,
        )

        query = (
            "SELECT id, event_type, task_id, agent_id, user_id, timestamp, details "
            f"FROM audit_events WHERE {where_sql} ORDER BY rowid ASC"
        )

        if limit is not None and limit >= 0:
            query += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        elif offset > 0:
            query += f" LIMIT -1 OFFSET {int(offset)}"

        with self._db.transaction() as conn:
            rows = conn.execute(query, params).fetchall()

        return [self._row_to_event(row) for row in rows]

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
        where_sql, params = self._build_query(
            task_id=task_id,
            agent_id=agent_id,
            event_type=event_type,
            user_id=user_id,
            start_time=start_time,
            end_time=end_time,
        )
        query = f"SELECT COUNT(*) as count FROM audit_events WHERE {where_sql}"

        with self._db.transaction() as conn:
            row = conn.execute(query, params).fetchone()
            return int(row["count"]) if row else 0

    def get_events_by_task(self, task_id: TaskId) -> list[AuditEvent]:
        """Compatibility convenience method for retrieving task events."""
        return self.get_events(task_id=task_id)

    def clear(self) -> None:
        """Clear all audit events; intended for isolated tests."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM audit_events")

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> AuditEvent:
        details_data = json.loads(row["details"])
        timestamp = datetime.fromisoformat(row["timestamp"])
        task_id = TaskId(id=UUID(row["task_id"])) if row["task_id"] else None
        agent_id = AgentId(id=row["agent_id"]) if row["agent_id"] else None

        return AuditEvent(
            id=UUID(row["id"]),
            event_type=AuditEventType(row["event_type"]),
            task_id=task_id,
            agent_id=agent_id,
            user_id=row["user_id"],
            timestamp=timestamp,
            details=details_data,
        )
