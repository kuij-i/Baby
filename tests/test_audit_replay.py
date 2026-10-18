"""Phase 17 follow-up: the TASK_CREATED audit event survives a failed write.

The task row (and its idempotency key) is committed before the creation audit event is
written, so an audit failure cannot roll the task back. These tests pin the recovery contract:
- the failed request returns 503 and never claims success;
- an idempotent retry returns the same task (no duplicate) and records the missing event;
- however many retries happen, concurrently or not, there is exactly one TASK_CREATED event;
- a persistent audit failure keeps failing truthfully;
- authentication, engagement scoping and principal isolation still apply to the retry;
- submissions without a key behave as before.
Both audit backends (in-memory and SQLite) are covered.
"""

import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from baby.api.app import app
from baby.audit import AuditLog, audit_log, task_created_event_id
from baby.configuration import settings
from baby.core import AuditEvent, AuditEventType, TaskId
from baby.observability import task_tracker, worker_tracker
from baby.persistence.database import DatabaseManager, db_manager
from baby.persistence.sqlite_audit import SQLiteAuditLog

OPERATOR_TOKEN = "op-token-for-tests-0123456789abcdef"
ADMIN_TOKEN = "admin-token-for-tests-0123456789abcdef"
ENG_A, ENG_B = "eng-a", "eng-b"
OP_AUTH = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
OP_A = {**OP_AUTH, "X-Engagement-ID": ENG_A}
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
BODY = {"title": "Report", "description": "Summarise the repository", "priority": 3}
KEY = "retry-key-0001"


@pytest.fixture(autouse=True)
def durable_env(tmp_path: Path) -> Iterator[Path]:
    original_url = db_manager.database_url
    db_manager.configure(f"sqlite:///{tmp_path / 'baby.db'}")
    db_manager.init_schema()
    task_tracker.clear()
    audit_log.use_backend(None)
    audit_log.clear()
    worker_tracker.clear()
    yield tmp_path / "baby.db"
    audit_log.use_backend(None)
    audit_log.clear()
    db_manager.configure(original_url)
    db_manager.init_schema()


@pytest.fixture(params=["memory", "sqlite"])
def audit_backend(request: pytest.FixtureRequest, durable_env: Path) -> str:
    if request.param == "sqlite":
        audit_log.use_backend(SQLiteAuditLog(db_manager))
    return str(request.param)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with patch.multiple(
        settings,
        api_require_auth=True,
        development_mode=False,
        api_auth_token=OPERATOR_TOKEN,
        api_admin_token=ADMIN_TOKEN,
        operator_engagements=ENG_A,
    ):
        yield TestClient(app, raise_server_exceptions=False)


class _AuditOutage:
    """Makes TASK_CREATED writes fail until ``heal()``.

    SQLite: a trigger aborts every insert into ``audit_events`` (a real failed SQL write).
    In-memory: the backend-less ``AuditLog.record_once`` raises.
    """

    def __init__(self, backend: str, db_file: Path) -> None:
        self._backend = backend
        self._db_file = db_file
        self._patch: Optional[Any] = None

    def start(self) -> "_AuditOutage":
        if self._backend == "sqlite":
            with closing(sqlite3.connect(self._db_file)) as conn, conn:
                conn.execute(
                    "CREATE TRIGGER audit_outage BEFORE INSERT ON audit_events "
                    "BEGIN SELECT RAISE(ABORT, 'simulated audit storage failure'); END;"
                )
        else:
            self._patch = patch.object(AuditLog, "record_once", side_effect=OSError("simulated audit failure"))
            self._patch.start()
        return self

    def heal(self) -> None:
        if self._backend == "sqlite":
            with closing(sqlite3.connect(self._db_file)) as conn, conn:
                conn.execute("DROP TRIGGER audit_outage;")
        elif self._patch is not None:
            self._patch.stop()
            self._patch = None


@pytest.fixture
def outage(audit_backend: str, durable_env: Path) -> Iterator[_AuditOutage]:
    failure = _AuditOutage(audit_backend, durable_env).start()
    yield failure
    if audit_backend == "memory":
        failure.heal()


def _submit(client: TestClient, key: Optional[str] = KEY, headers: Dict[str, str] = OP_A, **overrides: Any):
    extra = {"Idempotency-Key": key} if key is not None else {}
    return client.post("/api/v1/tasks/submit", json={**BODY, **overrides}, headers={**headers, **extra})


def _task_ids(db_file: Path) -> List[str]:
    with closing(sqlite3.connect(db_file)) as conn, conn:
        return [row[0] for row in conn.execute("SELECT task_id FROM task_records")]


def _created_events(task_id: Optional[str] = None) -> List[AuditEvent]:
    events = audit_log.get_events(event_type=AuditEventType.TASK_CREATED)
    return [e for e in events if task_id is None or str(e.task_id) == task_id]


def _fail_first_submission(client: TestClient, outage: _AuditOutage, durable_env: Path) -> str:
    """Submit while the audit store is down; return the task id that was nevertheless persisted."""
    failed = _submit(client)
    assert failed.status_code == 503
    (task_id,) = _task_ids(durable_env)
    return task_id


class TestFailedCreationAudit:
    def test_task_and_key_are_persisted_but_the_request_fails_truthfully(self, client, outage, durable_env):
        failed = _submit(client)
        assert failed.status_code == 503
        detail = failed.json()["detail"]
        (task_id,) = _task_ids(durable_env)
        assert task_id in detail and "audit record could not be written" in detail
        assert "same Idempotency-Key" in detail
        assert KEY not in failed.text  # the raw key is never echoed
        assert "replayed" not in failed.json() and "Idempotent-Replayed" not in failed.headers
        assert _created_events() == []  # nothing claims the task was audited

    def test_retry_creates_no_second_task_and_recovers_the_event(self, client, outage, durable_env):
        task_id = _fail_first_submission(client, outage, durable_env)
        outage.heal()
        retry = _submit(client)
        assert retry.status_code == 202
        assert retry.json()["task_id"] == task_id and retry.json()["replayed"] is True
        assert retry.headers["Idempotent-Replayed"] == "true"
        assert _task_ids(durable_env) == [task_id]
        (event,) = _created_events()
        assert str(event.task_id) == task_id and event.user_id == ENG_A
        assert event.id == task_created_event_id(TaskId(id=UUID(task_id)))
        assert event.details["recovered_on_replay"] is True and event.details["idempotency_key_used"] is True
        assert event.details["submitted_by"] == "operator-user"
        assert KEY not in repr(event.details)

    def test_repeated_retries_never_duplicate_the_event(self, client, outage, durable_env):
        task_id = _fail_first_submission(client, outage, durable_env)
        outage.heal()
        for _ in range(5):
            assert _submit(client).json()["task_id"] == task_id
        assert len(_created_events()) == 1 and _task_ids(durable_env) == [task_id]

    def test_concurrent_retries_record_exactly_one_event(self, client, outage, durable_env, audit_backend):
        task_id = _fail_first_submission(client, outage, durable_env)
        outage.heal()
        barrier = threading.Barrier(8)
        statuses: List[int] = []

        def retry() -> None:
            barrier.wait()
            statuses.append(_submit(client).status_code)

        threads = [threading.Thread(target=retry) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert statuses == [202] * 8
        assert len(_created_events(task_id)) == 1 and _task_ids(durable_env) == [task_id]

    def test_persistent_audit_failure_never_reports_success(self, client, outage, durable_env):
        task_id = _fail_first_submission(client, outage, durable_env)
        for _ in range(3):
            retry = _submit(client)
            assert retry.status_code == 503 and task_id in retry.json()["detail"]
        assert _created_events() == [] and _task_ids(durable_env) == [task_id]


class TestRecoveryKeepsIsolation:
    def test_unauthenticated_retry_is_rejected_and_recovers_nothing(self, client, outage, durable_env):
        _fail_first_submission(client, outage, durable_env)
        outage.heal()
        assert _submit(client, headers={"X-Engagement-ID": ENG_A}).status_code == 401
        assert _created_events() == []

    def test_engagement_expansion_is_rejected_and_recovers_nothing(self, client, outage, durable_env):
        _fail_first_submission(client, outage, durable_env)
        outage.heal()
        assert _submit(client, headers=OP_AUTH, engagement_id=ENG_B).status_code == 403
        assert _created_events() == []

    def test_another_principal_with_the_same_key_gets_its_own_task(self, client, outage, durable_env):
        operator_task = _fail_first_submission(client, outage, durable_env)
        outage.heal()
        admin = _submit(client, headers=ADMIN, engagement_id=ENG_A)
        assert admin.status_code == 202 and admin.json()["replayed"] is False
        assert admin.json()["task_id"] != operator_task
        assert _created_events(operator_task) == []  # the operator's event is theirs to recover
        (admin_event,) = _created_events(admin.json()["task_id"])
        assert admin_event.details["submitted_by"] == "admin-user" and "recovered_on_replay" not in admin_event.details


class TestUnchangedBehaviour:
    def test_successful_keyed_submission_and_replay_record_one_event(self, client, audit_backend, durable_env):
        first = _submit(client)
        assert first.status_code == 202 and first.json()["replayed"] is False
        assert _submit(client).json()["replayed"] is True
        (event,) = _created_events()
        assert str(event.task_id) == first.json()["task_id"] and "recovered_on_replay" not in event.details

    def test_submissions_without_key_are_unchanged(self, client, audit_backend, durable_env):
        first, second = _submit(client, key=None), _submit(client, key=None)
        assert first.status_code == second.status_code == 202
        assert first.json()["task_id"] != second.json()["task_id"]
        assert {str(e.task_id) for e in _created_events()} == {first.json()["task_id"], second.json()["task_id"]}
        assert all(e.details["idempotency_key_used"] is False for e in _created_events())

    def test_unkeyed_audit_failure_is_reported_without_inviting_a_duplicate(self, client, outage, durable_env):
        failed = _submit(client, key=None)
        assert failed.status_code == 503
        (task_id,) = _task_ids(durable_env)
        assert task_id in failed.json()["detail"] and "do not resubmit" in failed.json()["detail"]
        assert _created_events() == []


class TestRecordOnce:
    def _backends(self) -> List[Any]:
        return [AuditLog(), SQLiteAuditLog(db_manager)]

    def test_records_once_then_returns_none(self):
        task_id = TaskId()
        event_id = task_created_event_id(task_id)
        for backend in self._backends():
            first = backend.record_once(event_id, AuditEventType.TASK_CREATED, task_id=task_id, user_id=ENG_A)
            again = backend.record_once(event_id, AuditEventType.TASK_CREATED, task_id=task_id, details={"x": 1})
            assert first is not None and first.id == event_id and again is None
            (stored,) = backend.get_events(task_id=task_id)
            assert stored.id == event_id and stored.details == {}  # the second call wrote nothing

    def test_event_ids_are_deterministic_per_task(self):
        task_id = TaskId()
        assert task_created_event_id(task_id) == task_created_event_id(TaskId(id=task_id.id))
        assert task_created_event_id(task_id) != task_created_event_id(TaskId())

    def test_sqlite_record_once_survives_a_new_instance(self):
        task_id = TaskId()
        event_id = task_created_event_id(task_id)
        assert SQLiteAuditLog(db_manager).record_once(event_id, AuditEventType.TASK_CREATED, task_id=task_id)
        assert SQLiteAuditLog(db_manager).record_once(event_id, AuditEventType.TASK_CREATED, task_id=task_id) is None
        assert len(SQLiteAuditLog(db_manager).get_events(task_id=task_id)) == 1

    def test_facade_delegates_to_the_attached_backend(self):
        task_id = TaskId()
        audit_log.use_backend(SQLiteAuditLog(db_manager))
        assert audit_log.record_once(task_created_event_id(task_id), AuditEventType.TASK_CREATED, task_id=task_id)
        audit_log.use_backend(None)
        assert audit_log.get_events(task_id=task_id) == []  # written to SQLite, not the in-memory list
        assert len(SQLiteAuditLog(db_manager).get_events(task_id=task_id)) == 1

    def test_sqlite_record_once_raises_on_non_key_constraint_failure(self, tmp_path: Path):
        """Only a duplicate ID is "already recorded"; any other rejected insert must raise, never return None."""
        path = tmp_path / "constrained.db"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "CREATE TABLE audit_events (id TEXT PRIMARY KEY, event_type TEXT NOT NULL, task_id TEXT, "
                "agent_id TEXT, user_id TEXT, timestamp TEXT NOT NULL, "
                "details TEXT NOT NULL CHECK (length(details) < 10))"
            )
        manager = DatabaseManager(f"sqlite:///{path}")
        try:
            manager.init_schema()
            task_id = TaskId()
            with pytest.raises(sqlite3.IntegrityError):
                SQLiteAuditLog(manager).record_once(
                    task_created_event_id(task_id),
                    AuditEventType.TASK_CREATED,
                    task_id=task_id,
                    details={"x": "y" * 50},
                )
            assert SQLiteAuditLog(manager).get_events(task_id=task_id) == []
        finally:
            manager.close()
