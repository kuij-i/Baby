"""Phase 11: Comprehensive tests for durable SQLite persistence.

Verifies:
- DatabaseManager connection lifecycle, transactions, and rollback behavior
- Malformed configuration and fail-closed validation
- Concurrent transaction safety
- Durable SQLiteMemoryStore (CRUD, expiration, scope search, restart persistence)
- Durable SQLiteAuditLog (ordering, filtering, pagination, counting, engagement isolation, restart persistence)
- Durable SQLiteTaskStore (task record persistence, plan/approval child models, restart recovery)
"""

import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from baby.core import (
    AgentId,
    AuditEventType,
    MemoryRecord,
    Plan,
    PlanStep,
    Task,
    TaskId,
    TaskRecord,
    TaskStatus,
)
from baby.errors import ConfigurationError
from baby.observability.tasks import TaskTracker
from baby.persistence.database import DatabaseManager, parse_sqlite_path
from baby.persistence.sqlite_audit import SQLiteAuditLog
from baby.persistence.sqlite_memory import SQLiteMemoryStore
from baby.persistence.sqlite_tasks import SQLiteTaskStore


@pytest.fixture
def temp_db_manager():
    """Create an isolated, temporary SQLite database on disk for testing."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_file = Path(tmp_dir) / "test_persistence.db"
        mgr = DatabaseManager(f"sqlite:///{db_file}")
        mgr.init_schema()
        yield mgr
        mgr.close()


@pytest.fixture
def memory_db_manager():
    """Create an in-memory SQLite database manager."""
    mgr = DatabaseManager(":memory:")
    mgr.init_schema()
    yield mgr
    mgr.close()


# ============================================================================
# 1. DATABASE MANAGER & CONNECTION LIFECYCLE
# ============================================================================


class TestDatabaseManager:
    def test_parse_sqlite_path_formats(self):
        assert parse_sqlite_path("sqlite:///./baby.db") == "./baby.db"
        assert parse_sqlite_path("sqlite:////var/data/baby.db") == "/var/data/baby.db"
        assert parse_sqlite_path("sqlite:///:memory:") == ":memory:"
        assert parse_sqlite_path(":memory:") == ":memory:"
        assert parse_sqlite_path("./custom.db") == "./custom.db"

    def test_parse_sqlite_path_invalid_raises_configuration_error(self):
        with pytest.raises(ConfigurationError):
            parse_sqlite_path("")
        with pytest.raises(ConfigurationError):
            parse_sqlite_path("   ")
        with pytest.raises(ConfigurationError):
            parse_sqlite_path("sqlite:///")

    def test_transaction_commits_on_success(self, temp_db_manager):
        with temp_db_manager.transaction() as conn:
            conn.execute(
                "INSERT INTO memory_records (id, record_type, content, created_at) VALUES (?, ?, ?, ?)",
                ("rec-1", "fact", '{"a": 1}', datetime.utcnow().isoformat()),
            )

        with temp_db_manager.transaction() as conn:
            row = conn.execute("SELECT * FROM memory_records WHERE id = 'rec-1'").fetchone()
            assert row is not None
            assert row["id"] == "rec-1"

    def test_transaction_rolls_back_on_exception(self, temp_db_manager):
        with pytest.raises(ValueError):
            with temp_db_manager.transaction() as conn:
                conn.execute(
                    "INSERT INTO memory_records (id, record_type, content, created_at) VALUES (?, ?, ?, ?)",
                    ("rec-fail", "fact", '{"a": 1}', datetime.utcnow().isoformat()),
                )
                raise ValueError("Forced error to trigger rollback")

        # Confirm nothing was written
        with temp_db_manager.transaction() as conn:
            row = conn.execute("SELECT * FROM memory_records WHERE id = 'rec-fail'").fetchone()
            assert row is None

    def test_concurrency_multiple_threads(self, temp_db_manager):
        """Verify thread-safe concurrent writes without database lock errors."""
        errors = []

        def worker(idx: int):
            try:
                for i in range(10):
                    with temp_db_manager.transaction() as conn:
                        conn.execute(
                            "INSERT INTO memory_records (id, record_type, content, created_at) VALUES (?, ?, ?, ?)",
                            (f"thread-{idx}-{i}", "fact", '{"v": 1}', datetime.utcnow().isoformat()),
                        )
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        with temp_db_manager.transaction() as conn:
            row = conn.execute("SELECT COUNT(*) as count FROM memory_records").fetchone()
            assert row["count"] == 50


# ============================================================================
# 2. SQLITE MEMORY STORE
# ============================================================================


class TestSQLiteMemoryStore:
    def test_save_and_get(self, temp_db_manager):
        store = SQLiteMemoryStore(temp_db_manager)
        record = MemoryRecord(record_type="fact", content={"framework": "FastAPI"})
        saved = store.save(record)

        loaded = store.get(str(record.id))
        assert loaded is not None
        assert loaded.id == saved.id
        assert loaded.content == {"framework": "FastAPI"}
        assert loaded.record_type == "fact"

    def test_search_filters_and_limit(self, temp_db_manager):
        store = SQLiteMemoryStore(temp_db_manager)
        task_1 = TaskId()
        agent_1 = AgentId(id="coding-agent")

        store.save(MemoryRecord(task_id=task_1, agent_id=agent_1, record_type="decision", content={"k": "v1"}))
        store.save(MemoryRecord(task_id=task_1, agent_id=agent_1, record_type="decision", content={"k": "v2"}))
        store.save(MemoryRecord(task_id=task_1, record_type="fact", content={"k": "v3"}))

        results = store.search(task_id=task_1, agent_id=agent_1, record_type="decision")
        assert len(results) == 2

        limited = store.search(task_id=task_1, limit=1)
        assert len(limited) == 1

    def test_expired_records_not_returned(self, temp_db_manager):
        store = SQLiteMemoryStore(temp_db_manager)
        expired = MemoryRecord(
            record_type="temp",
            content={"temp": True},
            expires_at=datetime.utcnow() - timedelta(minutes=5),
        )
        store.save(expired)

        assert store.get(str(expired.id)) is None
        assert store.search(record_type="temp") == []

    def test_delete(self, temp_db_manager):
        store = SQLiteMemoryStore(temp_db_manager)
        record = MemoryRecord(record_type="fact", content={"val": 42})
        store.save(record)

        assert store.delete(str(record.id)) is True
        assert store.delete(str(record.id)) is False
        assert store.get(str(record.id)) is None

    def test_restart_persistence(self, temp_db_manager):
        """Simulate restart: records written to SQLite remain accessible in a new store instance."""
        store1 = SQLiteMemoryStore(temp_db_manager)
        rec = MemoryRecord(record_type="fact", content={"persistent": True})
        store1.save(rec)

        # Create new store instance connected to same manager
        store2 = SQLiteMemoryStore(temp_db_manager)
        loaded = store2.get(str(rec.id))
        assert loaded is not None
        assert loaded.content == {"persistent": True}


# ============================================================================
# 3. SQLITE AUDIT LOG
# ============================================================================


class TestSQLiteAuditLog:
    def test_record_and_get_events(self, temp_db_manager):
        audit = SQLiteAuditLog(temp_db_manager)
        task_id = TaskId()
        event = audit.record(
            event_type=AuditEventType.TASK_CREATED,
            task_id=task_id,
            user_id="operator-1",
            details={"priority": 1},
        )

        assert event.event_type == AuditEventType.TASK_CREATED
        assert event.user_id == "operator-1"

        events = audit.get_events(task_id=task_id)
        assert len(events) == 1
        assert events[0].id == event.id
        assert events[0].details == {"priority": 1}

    def test_audit_preserves_ordering(self, temp_db_manager):
        audit = SQLiteAuditLog(temp_db_manager)
        task_id = TaskId()

        audit.record(AuditEventType.TASK_CREATED, task_id=task_id)
        audit.record(AuditEventType.PLAN_CREATED, task_id=task_id)
        audit.record(AuditEventType.TASK_COMPLETED, task_id=task_id)

        events = audit.get_events(task_id=task_id)
        assert len(events) == 3
        assert events[0].event_type == AuditEventType.TASK_CREATED
        assert events[1].event_type == AuditEventType.PLAN_CREATED
        assert events[2].event_type == AuditEventType.TASK_COMPLETED

    def test_audit_engagement_isolation(self, temp_db_manager):
        audit = SQLiteAuditLog(temp_db_manager)

        audit.record(AuditEventType.TASK_CREATED, user_id="eng-alpha")
        audit.record(AuditEventType.TASK_CREATED, user_id="eng-beta")

        alpha_events = audit.get_events(user_id="eng-alpha")
        assert len(alpha_events) == 1
        assert alpha_events[0].user_id == "eng-alpha"

        beta_events = audit.get_events(user_id="eng-beta")
        assert len(beta_events) == 1
        assert beta_events[0].user_id == "eng-beta"

    def test_audit_pagination_and_counting(self, temp_db_manager):
        audit = SQLiteAuditLog(temp_db_manager)
        for i in range(10):
            audit.record(AuditEventType.TOOL_INVOKED, user_id="eng-1", details={"call": i})

        assert audit.count_events(user_id="eng-1") == 10

        page_1 = audit.get_events(user_id="eng-1", limit=4, offset=0)
        assert len(page_1) == 4
        assert page_1[0].details["call"] == 0

        page_2 = audit.get_events(user_id="eng-1", limit=4, offset=4)
        assert len(page_2) == 4
        assert page_2[0].details["call"] == 4

    def test_audit_restart_persistence(self, temp_db_manager):
        audit1 = SQLiteAuditLog(temp_db_manager)
        t_id = TaskId()
        audit1.record(AuditEventType.TASK_CREATED, task_id=t_id, details={"persisted": True})

        # New instance pointing to same SQLite database
        audit2 = SQLiteAuditLog(temp_db_manager)
        events = audit2.get_events(task_id=t_id)
        assert len(events) == 1
        assert events[0].details == {"persisted": True}


# ============================================================================
# 4. SQLITE TASK STORE & TASK TRACKER PERSISTENCE
# ============================================================================


class TestSQLiteTaskStore:
    def test_save_and_get_task(self, temp_db_manager):
        task_store = SQLiteTaskStore(temp_db_manager)
        task_id = TaskId()
        plan = Plan(
            task_id=task_id,
            steps=[PlanStep(step_id=1, description="Step 1")],
            reasoning="test reasoning",
        )
        record = TaskRecord(
            task_id=task_id,
            title="Durable Task",
            description="test description",
            status=TaskStatus.PENDING,
            user_id="eng-prod",
            plan=plan,
        )
        task_store.save_task(record)

        loaded = task_store.get_task(task_id)
        assert loaded is not None
        assert loaded.title == "Durable Task"
        assert loaded.user_id == "eng-prod"
        assert loaded.plan is not None
        assert len(loaded.plan.steps) == 1
        assert loaded.plan.steps[0].description == "Step 1"

    def test_task_tracker_writes_to_sqlite(self, temp_db_manager):
        task_store = SQLiteTaskStore(temp_db_manager)
        tracker = TaskTracker(task_store=task_store)

        task = Task(title="Tracker Task", description="desc", priority=5)
        tracker.register_task(task, status=TaskStatus.PENDING)
        tracker.update_status(task.id, TaskStatus.IN_PROGRESS)

        # Inspect raw SQLite database directly
        direct = task_store.get_task(task.id)
        assert direct is not None
        assert direct.status == TaskStatus.IN_PROGRESS
        assert direct.priority == 5

    def test_task_tracker_restart_recovery_from_sqlite(self, temp_db_manager):
        """Simulate crash and restart: in-progress tasks in SQLite are recovered as FAILED."""
        task_store = SQLiteTaskStore(temp_db_manager)
        tracker1 = TaskTracker(task_store=task_store)

        t1 = Task(title="Interrupted Task", description="desc")
        tracker1.register_task(t1, status=TaskStatus.PENDING)
        tracker1.update_status(t1.id, TaskStatus.IN_PROGRESS)

        # Simulate process termination: create fresh tracker instance pointing to same store
        tracker2 = TaskTracker(task_store=task_store)
        recovered = tracker2.recover_stale_tasks()

        assert len(recovered) == 1
        assert recovered[0].task_id == t1.id
        assert recovered[0].status == TaskStatus.FAILED

        # Verify that SQLite was also updated to FAILED
        persisted = task_store.get_task(t1.id)
        assert persisted is not None
        assert persisted.status == TaskStatus.FAILED
        assert "restart" in persisted.error.lower()
