"""Durable SQLite implementation of the MemoryStore interface."""

import json
import sqlite3
from copy import deepcopy
from datetime import datetime
from typing import Optional, Sequence
from uuid import UUID

from baby.core import AgentId, MemoryRecord, TaskId
from baby.memory.base import MemoryStore
from baby.persistence.database import DatabaseManager, db_manager


class SQLiteMemoryStore(MemoryStore):
    """Durable SQLite-backed memory store.

    Implements the MemoryStore interface with transactional SQLite persistence.
    Records survive application restarts and are safely isolated across scopes.
    """

    def __init__(self, manager: Optional[DatabaseManager] = None) -> None:
        self._db = manager or db_manager
        self._db.init_schema()

    def save(self, record: MemoryRecord) -> MemoryRecord:
        """Store or replace a memory record in SQLite."""
        record_id = str(record.id)
        task_id = str(record.task_id) if record.task_id else None
        agent_id = str(record.agent_id) if record.agent_id else None
        content_json = json.dumps(record.content)
        created_at_str = record.created_at.isoformat()
        expires_at_str = record.expires_at.isoformat() if record.expires_at else None

        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO memory_records (id, task_id, agent_id, record_type, content, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    task_id=excluded.task_id,
                    agent_id=excluded.agent_id,
                    record_type=excluded.record_type,
                    content=excluded.content,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at;
                """,
                (
                    record_id,
                    task_id,
                    agent_id,
                    record.record_type,
                    content_json,
                    created_at_str,
                    expires_at_str,
                ),
            )

        return deepcopy(record)

    def get(self, record_id: str) -> Optional[MemoryRecord]:
        """Return a non-expired record by UUID string, if present."""
        query = (
            "SELECT id, task_id, agent_id, record_type, content, created_at, expires_at "
            "FROM memory_records WHERE id = ?"
        )
        with self._db.transaction() as conn:
            row = conn.execute(query, (record_id,)).fetchone()

        if row is None:
            return None

        record = self._row_to_record(row)
        if self._is_expired(record):
            return None

        return record

    def search(
        self,
        *,
        task_id: Optional[TaskId] = None,
        agent_id: Optional[AgentId] = None,
        record_type: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[MemoryRecord]:
        """Return newest matching non-expired records up to ``limit``."""
        if limit < 1:
            raise ValueError("limit must be at least 1")

        query = (
            "SELECT id, task_id, agent_id, record_type, content, created_at, expires_at "
            "FROM memory_records WHERE 1=1"
        )
        params = []

        if task_id is not None:
            query += " AND task_id = ?"
            params.append(str(task_id))

        if agent_id is not None:
            query += " AND agent_id = ?"
            params.append(str(agent_id))

        if record_type is not None:
            query += " AND record_type = ?"
            params.append(record_type)

        query += " ORDER BY created_at DESC"

        with self._db.transaction() as conn:
            rows = conn.execute(query, params).fetchall()

        results = []
        for row in rows:
            record = self._row_to_record(row)
            if not self._is_expired(record):
                results.append(record)
                if len(results) >= limit:
                    break

        return results

    def delete(self, record_id: str) -> bool:
        """Delete a record by UUID string."""
        with self._db.transaction() as conn:
            cursor = conn.execute("DELETE FROM memory_records WHERE id = ?", (record_id,))
            return cursor.rowcount > 0

    def clear(self) -> None:
        """Remove all records; intended for testing and controlled resets."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM memory_records")

    @staticmethod
    def _is_expired(record: MemoryRecord) -> bool:
        return record.expires_at is not None and record.expires_at <= datetime.utcnow()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
        content_data = json.loads(row["content"])
        created_at = datetime.fromisoformat(row["created_at"])
        expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None

        task_id = TaskId(id=UUID(row["task_id"])) if row["task_id"] else None
        agent_id = AgentId(id=row["agent_id"]) if row["agent_id"] else None

        return MemoryRecord(
            id=UUID(row["id"]),
            task_id=task_id,
            agent_id=agent_id,
            record_type=row["record_type"],
            content=content_data,
            created_at=created_at,
            expires_at=expires_at,
        )
