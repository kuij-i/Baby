"""Phase 17 / D7: UTC timestamp and Pydantic v2 configuration hygiene (supersedes PR #2).

Baby's canonical timestamp is *naive UTC*: every persisted row, lease and expiry comparison
and API response already uses it. These tests pin that contract:
- no deprecated ``datetime.utcnow()`` in ``baby/`` and no deprecation warnings on import;
- model defaults are naive UTC "now";
- timezone-aware input (e.g. ``...Z``) is normalised to naive UTC at the model and API
  boundary, so mixed naive/aware comparisons can never raise ``TypeError`` (or a 500);
- legacy naive values keep working;
- Pydantic v2 ``ConfigDict`` / ``SettingsConfigDict`` behaviour is unchanged.
"""

import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from baby.api.app import app
from baby.audit import audit_log
from baby.configuration import Settings, settings
from baby.core import (
    AgentId,
    AgentResult,
    ApprovalRequest,
    Artifact,
    AuditEvent,
    AuditEventType,
    MemoryRecord,
    Plan,
    PlanStep,
    Task,
    TaskId,
    TaskRecord,
    TaskStatus,
    VerificationResult,
    WorkerInfo,
    to_naive_utc,
    utc_now,
)
from baby.memory.in_memory import InMemoryStore
from baby.permissions.approvals import approval_expired

REPO = Path(__file__).resolve().parent.parent


def _close_to_now(value: datetime) -> bool:
    return value.tzinfo is None and abs(value - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(seconds=5)


class TestHelpers:
    def test_utc_now_is_naive_utc(self):
        assert _close_to_now(utc_now())

    def test_to_naive_utc_converts_aware_values(self):
        aware = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        plus_two = datetime(2026, 1, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
        assert to_naive_utc(aware) == datetime(2026, 1, 1, 12, 0)
        assert to_naive_utc(plus_two) == datetime(2026, 1, 1, 12, 0)

    def test_to_naive_utc_keeps_legacy_naive_values(self):
        legacy = datetime(2025, 6, 1, 8, 30)
        assert to_naive_utc(legacy) is legacy


_DEFAULTS: "list[tuple[str, Callable[[], Any], str]]" = [
    ("Task", lambda: Task(title="t", description="d"), "created_at"),
    ("Plan", lambda: Plan(task_id=TaskId(), steps=[PlanStep(step_id=1, description="s")], reasoning="r"), "created_at"),
    ("AgentResult", lambda: AgentResult(step_id=1, agent_id=AgentId(id="a"), success=True), "timestamp"),
    (
        "VerificationResult",
        lambda: VerificationResult(agent_result_id=1, verified=True, verification_method="m"),
        "timestamp",
    ),
    (
        "ApprovalRequest",
        lambda: ApprovalRequest(
            task_id=TaskId(), agent_id=AgentId(id="a"), action_type="x", reason="r", risk_level="h"
        ),
        "created_at",
    ),
    (
        "Artifact",
        lambda: Artifact(task_id=TaskId(), agent_id=AgentId(id="a"), name="n", artifact_type="t", content=1),
        "created_at",
    ),
    ("MemoryRecord", lambda: MemoryRecord(record_type="fact", content={}), "created_at"),
    ("AuditEvent", lambda: AuditEvent(event_type=AuditEventType.TASK_CREATED), "timestamp"),
    ("TaskRecord", lambda: TaskRecord(task_id=TaskId(), title="t", description="d"), "created_at"),
    ("WorkerInfo", lambda: WorkerInfo(worker_id="w"), "last_heartbeat_at"),
]


class TestModelTimestamps:
    @pytest.mark.parametrize("name,factory,field", _DEFAULTS, ids=[d[0] for d in _DEFAULTS])
    def test_defaults_are_naive_utc_now(self, name, factory, field):
        assert _close_to_now(getattr(factory(), field))

    def test_aware_input_is_normalised_to_naive_utc(self):
        task = Task(
            title="t", description="d", created_at=datetime(2026, 1, 1, 14, 0, tzinfo=timezone(timedelta(hours=2)))
        )
        assert task.created_at == datetime(2026, 1, 1, 12, 0) and task.created_at.tzinfo is None
        parsed = Task.model_validate({"title": "t", "description": "d", "created_at": "2026-01-01T12:00:00Z"})
        assert parsed.created_at == datetime(2026, 1, 1, 12, 0)

    def test_aware_approval_timestamps_never_raise_in_expiry_checks(self):
        # PR #2's aware defaults would raise TypeError here against naive "now"; normalisation prevents it.
        request = ApprovalRequest(
            task_id=TaskId(),
            agent_id=AgentId(id="a"),
            action_type="x",
            reason="r",
            risk_level="h",
            created_at=datetime.now(timezone.utc) - timedelta(hours=25),
        )
        assert approval_expired(request, utc_now(), timedelta(hours=24)) is True

    def test_audit_event_keeps_enum_values(self):
        event = AuditEvent(event_type=AuditEventType.TASK_CREATED)
        assert event.event_type == "task_created" and event.model_dump()["event_type"] == "task_created"


class TestMemoryExpiry:
    @pytest.mark.parametrize(
        "expires_at,expired",
        [
            (utc_now() - timedelta(minutes=1), True),  # legacy naive, past
            (utc_now() + timedelta(minutes=10), False),  # legacy naive, future
            (datetime.now(timezone.utc) - timedelta(minutes=1), True),  # aware, past
            (datetime.now(timezone.utc) + timedelta(minutes=10), False),  # aware, future
        ],
        ids=["naive-past", "naive-future", "aware-past", "aware-future"],
    )
    def test_expiry_handles_naive_and_aware_values(self, expires_at, expired):
        store = InMemoryStore()
        store.save(MemoryRecord(record_type="fact", content={"k": 1}, expires_at=expires_at))
        assert (store.search(limit=10) == []) is expired


class TestAuditTimeFilters:
    @pytest.fixture
    def client(self):
        with patch.multiple(
            settings,
            api_require_auth=False,
            development_mode=True,
            persistence_backend="in_memory",
            audit_storage_type="in_memory",
        ):
            audit_log.clear()
            yield TestClient(app, raise_server_exceptions=False)
            audit_log.clear()

    @pytest.mark.parametrize(
        "start", ["2020-01-01T00:00:00", "2020-01-01T00:00:00Z", "2020-01-01T02:00:00+02:00"], ids=str
    )
    def test_aware_or_naive_start_time_is_accepted(self, client, start):
        audit_log.record(AuditEventType.TASK_CREATED, user_id="eng")
        resp = client.get("/api/v1/audit", params={"start_time": start})
        assert resp.status_code == 200 and len(resp.json()) == 1  # was 500 for "...Z" before

    def test_offset_is_applied_not_ignored(self, client):
        audit_log.record(AuditEventType.TASK_CREATED, user_id="eng")
        ahead = (datetime.now(timezone.utc) + timedelta(minutes=30)).astimezone(timezone(timedelta(hours=-5)))
        resp = client.get("/api/v1/audit", params={"start_time": ahead.isoformat()})
        assert resp.status_code == 200 and resp.json() == []


class TestPydanticV2Configuration:
    def test_settings_use_settings_config_dict(self):
        assert Settings.model_config.get("env_file") == ".env"
        assert Settings.model_config.get("case_sensitive") is False

    def test_settings_env_lookup_stays_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("coding_max_iterations", "7")
        assert Settings().coding_max_iterations == 7

    def test_no_deprecated_utcnow_in_baby_sources(self):
        pattern = re.compile(r"(?<!`)datetime\.utcnow")  # ignores the ``datetime.utcnow()`` docstring mention
        offenders = [
            str(p.relative_to(REPO)) for p in (REPO / "baby").rglob("*.py") if pattern.search(p.read_text("utf-8"))
        ]
        assert offenders == []

    def test_importing_baby_emits_no_deprecation_warnings(self):
        code = "import baby.configuration, baby.core, baby.api.app, baby.workers.runtime, baby.memory"
        result = subprocess.run(
            [sys.executable, "-W", "error::DeprecationWarning", "-c", code],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-2000:]

    def test_task_status_round_trip_unchanged(self):
        record = TaskRecord(task_id=TaskId(), title="t", description="d", status=TaskStatus.PENDING)
        assert TaskRecord.model_validate(record.model_dump()).created_at == record.created_at
