"""Phase 17 / D2: optional Idempotency-Key support for task submission.

Covers: backward compatibility without the header, first submission, equivalent replay,
conflicting replay (409), key validation, principal/engagement scoping and authorization,
concurrent submissions (threads and separate processes), restart survival, migration of
an existing v2 database, and that a failed transaction never leaves a key reserved
without its task.
"""

import multiprocessing
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from baby.api.app import app
from baby.audit import audit_log
from baby.configuration import settings
from baby.core import AuditEventType, Task
from baby.errors import IdempotencyConflictError
from baby.observability import task_tracker, worker_tracker
from baby.persistence.database import SCHEMA_VERSION, DatabaseManager, db_manager
from baby.persistence.sqlite_tasks import SQLiteTaskStore
from baby.persistence.sqlite_workers import SQLiteWorkerStore
from baby.workers.queue import DurableTaskQueue, IdempotencyScope, submission_fingerprint
from tests.worker_helpers import idempotent_submit_racer

OPERATOR_TOKEN = "op-token-for-tests-0123456789abcdef"
ADMIN_TOKEN = "admin-token-for-tests-0123456789abcdef"
ENG_A, ENG_B = "eng-a", "eng-b"
OP_AUTH = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
OP_A = {**OP_AUTH, "X-Engagement-ID": ENG_A}
ADMIN = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
BODY = {"title": "Report", "description": "Summarise the repository", "priority": 3}


@pytest.fixture(autouse=True)
def durable_env(tmp_path: Path):
    original_url = db_manager.database_url
    db_manager.configure(f"sqlite:///{tmp_path / 'baby.db'}")
    db_manager.init_schema()
    task_tracker.clear()
    audit_log.clear()
    worker_tracker.clear()
    yield tmp_path / "baby.db"
    audit_log.clear()
    db_manager.configure(original_url)
    db_manager.init_schema()


@pytest.fixture
def client():
    with patch.multiple(
        settings,
        api_require_auth=True,
        development_mode=False,
        api_auth_token=OPERATOR_TOKEN,
        api_admin_token=ADMIN_TOKEN,
        operator_engagements=ENG_A,
    ):
        yield TestClient(app, raise_server_exceptions=False)


def _submit(client: TestClient, key: Optional[str], headers: Dict[str, str] = OP_A, **overrides: Any):
    body = {**BODY, **overrides}
    extra = {"Idempotency-Key": key} if key is not None else {}
    return client.post("/api/v1/tasks/submit", json=body, headers={**headers, **extra})


def _task_count(db_file: Path) -> int:
    with closing(sqlite3.connect(db_file)) as conn, conn:
        return int(conn.execute("SELECT COUNT(*) FROM task_records").fetchone()[0])


def _key_rows(db_file: Path) -> List[sqlite3.Row]:
    with closing(sqlite3.connect(db_file)) as conn, conn:
        conn.row_factory = sqlite3.Row
        return list(conn.execute("SELECT * FROM task_idempotency_keys"))


def _queue(manager: Optional[DatabaseManager] = None) -> DurableTaskQueue:
    store = SQLiteTaskStore(manager) if manager else task_tracker.store
    assert store is not None
    return DurableTaskQueue(store, SQLiteWorkerStore(store.db))


def _task(description: str = "same request", engagement: str = "eng-race") -> Task:
    return Task(title="race", description=description, priority=1, user_id=engagement)


# ============================================================================
# API behaviour
# ============================================================================


class TestSubmissionSemantics:
    def test_requests_without_key_are_unchanged(self, client, durable_env):
        first, second = _submit(client, None), _submit(client, None)
        assert first.status_code == second.status_code == 202
        assert first.json()["task_id"] != second.json()["task_id"]
        assert first.json()["replayed"] is False and "Idempotent-Replayed" not in first.headers
        assert _task_count(durable_env) == 2 and _key_rows(durable_env) == []

    def test_first_submission_then_equivalent_replay_returns_original(self, client, durable_env):
        first = _submit(client, "key-123")
        assert first.status_code == 202 and first.json()["replayed"] is False
        replay = _submit(client, "key-123")
        assert replay.status_code == 202
        assert replay.json()["task_id"] == first.json()["task_id"]
        assert replay.json()["replayed"] is True and replay.headers["Idempotent-Replayed"] == "true"
        assert _task_count(durable_env) == 1
        created = audit_log.get_events(event_type=AuditEventType.TASK_CREATED)
        assert len(created) == 1 and created[0].details["idempotency_key_used"] is True

    def test_engagement_from_header_or_body_is_the_same_request(self, client):
        first = _submit(client, "key-eng")
        replay = _submit(client, "key-eng", headers=OP_AUTH, engagement_id=ENG_A)
        assert replay.json()["task_id"] == first.json()["task_id"] and replay.json()["replayed"] is True

    @pytest.mark.parametrize(
        "change", [{"description": "something else"}, {"priority": 4}, {"title": "Other"}], ids=str
    )
    def test_conflicting_replay_returns_409_and_creates_nothing(self, client, durable_env, change):
        first = _submit(client, "key-conflict")
        conflict = _submit(client, "key-conflict", **change)
        assert conflict.status_code == 409
        assert "different request" in conflict.json()["detail"]
        assert "key-conflict" not in conflict.text  # the key is never echoed back
        assert _task_count(durable_env) == 1
        assert _submit(client, "key-conflict").json()["task_id"] == first.json()["task_id"]

    def test_replay_reports_the_current_task_status(self, client):
        first = _submit(client, "key-status").json()
        _queue().request_cancel(_tid(first))  # PENDING -> CANCELLED through the state machine
        replay = _submit(client, "key-status").json()
        assert replay["status"] == "cancelled" and replay["replayed"] is True

    @pytest.mark.parametrize("key", ["", "a" * 256, "has space", "tab\tkey"], ids=["empty", "too-long", "space", "tab"])
    def test_invalid_keys_are_rejected_with_422(self, client, durable_env, key):
        assert _submit(client, key).status_code == 422
        assert _task_count(durable_env) == 0

    def test_non_ascii_key_is_rejected_not_500(self, client, durable_env):
        resp = client.post(
            "/api/v1/tasks/submit",
            json=BODY,
            headers={**OP_A, "Idempotency-Key": "clé".encode("latin-1")},  # type: ignore[dict-item]
        )
        assert resp.status_code == 422 and _task_count(durable_env) == 0

    def test_longest_valid_key_is_accepted(self, client):
        key = "k" * 255
        assert _submit(client, key).json()["task_id"] == _submit(client, key).json()["task_id"]

    def test_raw_key_is_never_stored(self, client, durable_env):
        _submit(client, "secret-looking-key-value")
        (row,) = _key_rows(durable_env)
        assert row["key_hash"] != "secret-looking-key-value" and len(row["key_hash"]) == 64
        assert b"secret-looking-key-value" not in durable_env.read_bytes()

    def test_openapi_documents_the_header_and_replayed_flag(self):
        schema = app.openapi()
        params = schema["paths"]["/api/v1/tasks/submit"]["post"]["parameters"]
        assert any(p["name"] == "Idempotency-Key" and p["in"] == "header" for p in params)
        assert "replayed" in schema["components"]["schemas"]["TaskSubmitResponse"]["properties"]


def _tid(view: Dict[str, Any]):  # type: ignore[no-untyped-def]
    from uuid import UUID

    from baby.core import TaskId

    return TaskId(id=UUID(view["task_id"]))


class TestScopeAndAuthorization:
    def test_same_key_from_another_principal_never_returns_their_task(self, client, durable_env):
        operator_task = _submit(client, "shared-key").json()["task_id"]
        admin = _submit(client, "shared-key", headers=ADMIN, engagement_id=ENG_A)
        assert admin.status_code == 202 and admin.json()["replayed"] is False
        assert admin.json()["task_id"] != operator_task
        assert _task_count(durable_env) == 2

    def test_operator_cannot_reach_another_engagements_task_by_replaying_its_key(self, client, durable_env):
        admin_task = _submit(client, "admin-key", headers=ADMIN, engagement_id=ENG_B).json()
        assert admin_task["engagement_id"] == ENG_B
        # Asking for engagement B is outside the operator's server-side scope.
        denied = _submit(client, "admin-key", headers=OP_AUTH, engagement_id=ENG_B)
        assert denied.status_code == 403 and admin_task["task_id"] not in denied.text
        # Within its own engagement the same key is a fresh, unrelated submission.
        own = _submit(client, "admin-key")
        assert own.status_code == 202 and own.json()["task_id"] != admin_task["task_id"]
        assert own.json()["engagement_id"] == ENG_A and own.json()["replayed"] is False
        assert client.get(f"/api/v1/tasks/{admin_task['task_id']}", headers=OP_A).status_code == 404

    def test_keys_are_scoped_per_engagement(self, client):
        with patch.object(settings, "operator_engagements", f"{ENG_A},{ENG_B}"):
            in_a = _submit(client, "per-eng", engagement_id=ENG_A).json()
            in_b = _submit(client, "per-eng", engagement_id=ENG_B).json()
        assert in_a["task_id"] != in_b["task_id"] and not in_b["replayed"]

    def test_unauthenticated_request_with_key_is_rejected_before_any_lookup(self, client, durable_env):
        resp = client.post("/api/v1/tasks/submit", json=BODY, headers={"Idempotency-Key": "k"})
        assert resp.status_code == 401 and _key_rows(durable_env) == []


# ============================================================================
# Queue-level guarantees: atomicity, concurrency, restarts, migration
# ============================================================================


class TestQueueGuarantees:
    def test_scope_must_match_task_engagement(self):
        with pytest.raises(ValueError):
            _queue().submit_idempotent(_task(engagement="eng-x"), IdempotencyScope("p", "eng-y", "k"))

    def test_conflict_raises_and_writes_nothing(self, durable_env):
        queue, scope = _queue(), IdempotencyScope("p", "eng-race", "k")
        queue.submit_idempotent(_task(), scope)
        with pytest.raises(IdempotencyConflictError):
            queue.submit_idempotent(_task(description="different"), scope)
        assert _task_count(durable_env) == 1

    def test_fingerprint_covers_the_request_fields_only(self):
        a, b = _task(), _task()
        assert a.id != b.id and submission_fingerprint(a) == submission_fingerprint(b)
        assert submission_fingerprint(a) != submission_fingerprint(_task(description="x"))
        assert submission_fingerprint(a) != submission_fingerprint(_task(engagement="eng-other"))

    def test_failed_key_insert_rolls_back_the_task(self, durable_env):
        queue, scope = _queue(), IdempotencyScope("p", "eng-race", "k-fail")
        with patch("baby.workers.queue.ts", side_effect=RuntimeError("disk full")):
            with pytest.raises(RuntimeError):
                queue.submit_idempotent(_task(), scope)
        assert _task_count(durable_env) == 0 and _key_rows(durable_env) == []
        record, replayed = queue.submit_idempotent(_task(), scope)
        assert replayed is False and _task_count(durable_env) == 1

    def test_failed_task_write_reserves_no_key(self, durable_env):
        queue, scope = _queue(), IdempotencyScope("p", "eng-race", "k-fail-2")
        with patch.object(SQLiteTaskStore, "write_record", side_effect=sqlite3.OperationalError("locked")):
            with pytest.raises(sqlite3.OperationalError):
                queue.submit_idempotent(_task(), scope)
        assert _key_rows(durable_env) == [] and _task_count(durable_env) == 0

    def test_concurrent_threads_create_exactly_one_task(self, durable_env):
        queue, scope = _queue(), IdempotencyScope("p", "eng-race", "k-threads")
        barrier, results, errors = threading.Barrier(8), [], []

        def worker() -> None:
            try:
                barrier.wait()
                results.append(queue.submit_idempotent(_task(), scope))
            except Exception as exc:  # pragma: no cover - surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert errors == []
        assert len({str(r.task_id) for r, _ in results}) == 1
        assert sorted(replayed for _, replayed in results) == [False] + [True] * 7
        assert _task_count(durable_env) == 1

    def test_concurrent_processes_create_exactly_one_task(self, durable_env):
        ctx = multiprocessing.get_context("spawn")
        count = 4
        barrier, results = ctx.Barrier(count), ctx.Queue()
        url = f"sqlite:///{durable_env}"
        procs = [
            ctx.Process(target=idempotent_submit_racer, args=(url, "k-processes", barrier, results))
            for _ in range(count)
        ]
        for p in procs:
            p.start()
        outcomes = [results.get(timeout=60) for _ in range(count)]
        for p in procs:
            p.join(timeout=30)
            assert p.exitcode == 0
        assert len({task_id for task_id, _ in outcomes}) == 1
        assert sorted(replayed for _, replayed in outcomes) == [False] + [True] * (count - 1)
        assert _task_count(durable_env) == 1

    def test_replay_survives_a_database_restart(self, durable_env):
        scope = IdempotencyScope("p", "eng-race", "k-restart")
        first, _ = _queue().submit_idempotent(_task(), scope)
        db_manager.close()  # simulate the API/worker process going away
        fresh = DatabaseManager(f"sqlite:///{durable_env}")
        try:
            record, replayed = _queue(fresh).submit_idempotent(_task(), scope)
        finally:
            fresh.close()
        assert replayed is True and record.task_id == first.task_id

    def test_v2_database_is_migrated_without_losing_data(self, tmp_path):
        legacy_file = tmp_path / "legacy.db"
        current = DatabaseManager(f"sqlite:///{legacy_file}")
        current.init_schema()
        _queue(current).submit(Task(title="old", description="kept", user_id="eng-old"))
        with current.transaction() as conn:  # roll the schema back to v2
            conn.execute("DROP TABLE task_idempotency_keys")
            conn.execute("PRAGMA user_version = 2")
        current.close()

        upgraded = DatabaseManager(f"sqlite:///{legacy_file}")
        try:
            upgraded.init_schema()
            assert upgraded.schema_version() == SCHEMA_VERSION == 3
            store = SQLiteTaskStore(upgraded)
            assert [r.description for r in store.list_tasks()] == ["kept"]
            record, replayed = _queue(upgraded).submit_idempotent(
                _task(engagement="eng-old"), IdempotencyScope("p", "eng-old", "k-migrated")
            )
            assert replayed is False and len(store.list_tasks()) == 2
            upgraded.init_schema()  # idempotent
            assert upgraded.schema_version() == 3
        finally:
            upgraded.close()
