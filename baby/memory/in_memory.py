"""Simple in-memory memory store for the initial BABY implementation."""

from copy import deepcopy
from datetime import datetime, timezone
from typing import Optional, Sequence

from baby.core import AgentId, MemoryRecord, TaskId
from baby.memory.base import MemoryStore


class InMemoryStore(MemoryStore):
    """Deterministic memory store suitable for development and tests.

    Records are copied on write and read so callers cannot mutate stored state
    without going through the memory interface.
    """

    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}

    def save(self, record: MemoryRecord) -> MemoryRecord:
        """Store or replace a record."""
        stored = deepcopy(record)
        self._records[str(stored.id)] = stored
        return deepcopy(stored)

    def get(self, record_id: str) -> Optional[MemoryRecord]:
        """Return a non-expired record by UUID string, if present."""
        record = self._records.get(record_id)
        if record is None or self._is_expired(record):
            return None
        return deepcopy(record)

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

        records = [
            record
            for record in self._records.values()
            if not self._is_expired(record)
            and (task_id is None or record.task_id == task_id)
            and (agent_id is None or record.agent_id == agent_id)
            and (record_type is None or record.record_type == record_type)
        ]
        records.sort(key=lambda record: record.created_at, reverse=True)
        return [deepcopy(record) for record in records[:limit]]

    def delete(self, record_id: str) -> bool:
        """Delete a record by UUID string."""
        return self._records.pop(record_id, None) is not None

    def clear(self) -> None:
        """Remove all records; intended for tests and controlled resets."""
        self._records.clear()

    @staticmethod
    def _is_expired(record: MemoryRecord) -> bool:
        if record.expires_at is None:
            return False

        expires_at = record.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        return expires_at <= datetime.now(timezone.utc)


memory_store = InMemoryStore()
