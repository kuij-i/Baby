"""Tests for the memory subsystem."""

from datetime import datetime, timedelta, timezone

import pytest

from baby.core import AgentId, MemoryRecord, TaskId
from baby.memory import InMemoryStore


class TestInMemoryStore:
    def setup_method(self) -> None:
        self.store = InMemoryStore()

    def test_save_and_get_returns_copy(self) -> None:
        record = MemoryRecord(record_type="fact", content={"value": "original"})
        saved = self.store.save(record)
        loaded = self.store.get(str(record.id))

        assert loaded is not None
        assert loaded.id == saved.id
        loaded.content["value"] = "changed"
        assert self.store.get(str(record.id)).content["value"] == "original"

    def test_search_filters_by_scope(self) -> None:
        task_id = TaskId()
        agent_id = AgentId(id="coding-agent")
        self.store.save(
            MemoryRecord(
                task_id=task_id,
                agent_id=agent_id,
                record_type="decision",
                content={"choice": "sqlite"},
            )
        )
        self.store.save(MemoryRecord(record_type="fact", content={"value": "other"}))

        results = self.store.search(task_id=task_id, agent_id=agent_id, record_type="decision")
        assert len(results) == 1
        assert results[0].content["choice"] == "sqlite"

    def test_search_limit_is_enforced(self) -> None:
        for index in range(3):
            self.store.save(MemoryRecord(record_type="history", content={"index": index}))

        assert len(self.store.search(limit=2)) == 2

    def test_invalid_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="limit"):
            self.store.search(limit=0)

    def test_expired_records_are_not_returned(self) -> None:
        record = MemoryRecord(
            record_type="temporary",
            content={"value": "expired"},
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        self.store.save(record)

        assert self.store.get(str(record.id)) is None
        assert self.store.search() == []

    def test_naive_expired_records_are_treated_as_utc(self) -> None:
        record = MemoryRecord(
            record_type="temporary",
            content={"value": "expired"},
            expires_at=datetime.now() - timedelta(seconds=1),
        )
        self.store.save(record)

        assert self.store.get(str(record.id)) is None

    def test_delete_reports_presence(self) -> None:
        record = self.store.save(MemoryRecord(record_type="fact", content={"value": 1}))

        assert self.store.delete(str(record.id)) is True
        assert self.store.delete(str(record.id)) is False
        assert self.store.get(str(record.id)) is None
