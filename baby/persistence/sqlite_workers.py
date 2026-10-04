"""Durable SQLite registry of worker processes.

Each worker process registers exactly one row under a unique, never-reused
``worker_id``. The row carries the worker's lifecycle status, heartbeat, current
task and bounded operational counters. Secrets are never stored here: metadata is
restricted to an allow-list of operational keys.
"""

import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from baby.core import TaskId, WorkerInfo, WorkerStatus
from baby.persistence.database import DatabaseManager, db_manager

# Only these metadata keys are persisted; anything else (e.g. environment, tokens) is dropped.
ALLOWED_WORKER_METADATA_KEYS = frozenset({"hostname", "pid", "platform", "python", "start_method", "slot"})

# Statuses that represent a worker which is expected to be running.
LIVE_WORKER_STATUSES = (WorkerStatus.STARTING, WorkerStatus.IDLE, WorkerStatus.BUSY, WorkerStatus.STOPPING)


def ts(value: datetime) -> str:
    """Serialize a UTC timestamp with fixed precision so string comparison orders correctly."""
    return value.isoformat(timespec="microseconds")


def parse_ts(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def sanitize_worker_metadata(metadata: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keep only allow-listed, scalar operational metadata."""
    clean: Dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        if key in ALLOWED_WORKER_METADATA_KEYS and isinstance(value, (str, int, float, bool)):
            clean[key] = value
    return clean


class SQLiteWorkerStore:
    """Durable worker registration, heartbeat and status storage."""

    def __init__(self, manager: Optional[DatabaseManager] = None) -> None:
        self._db = manager or db_manager
        self._db.init_schema()

    @property
    def db(self) -> DatabaseManager:
        return self._db

    def register(
        self,
        worker_id: str,
        hostname: str,
        pid: int,
        now: datetime,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> WorkerInfo:
        """Insert a new worker identity in STARTING state.

        Raises:
            sqlite3.IntegrityError: If the worker_id already exists (identities are never reused).
        """
        clean = sanitize_worker_metadata({**(metadata or {}), "hostname": hostname, "pid": pid})
        with self._db.immediate_transaction() as conn:
            conn.execute(
                """
                INSERT INTO workers (worker_id, hostname, pid, status, started_at, last_heartbeat_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (worker_id, hostname, pid, WorkerStatus.STARTING.value, ts(now), ts(now), json.dumps(clean)),
            )
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return self._row_to_info(row)

    def heartbeat(self, worker_id: str, now: datetime) -> bool:
        """Record a heartbeat. Returns False if the worker is no longer considered live.

        A worker that recovery already marked STALE/ERROR/STOPPED cannot revive itself by
        heartbeating; it must stop and a new identity must be registered.
        """
        placeholders = ",".join("?" for _ in LIVE_WORKER_STATUSES)
        with self._db.immediate_transaction() as conn:
            cur = conn.execute(
                f"UPDATE workers SET last_heartbeat_at = ? WHERE worker_id = ? AND status IN ({placeholders})",
                (ts(now), worker_id, *[s.value for s in LIVE_WORKER_STATUSES]),
            )
            return cur.rowcount == 1

    def set_status(
        self,
        worker_id: str,
        status: WorkerStatus,
        now: datetime,
        current_task_id: Optional[TaskId] = None,
        error: Optional[str] = None,
        only_if_live: bool = True,
    ) -> bool:
        """Update a worker's status (and current task). Returns False if no row changed."""
        stopped_at = ts(now) if status in (WorkerStatus.STOPPED, WorkerStatus.ERROR, WorkerStatus.STALE) else None
        query = (
            "UPDATE workers SET status = ?, current_task_id = ?, last_error = COALESCE(?, last_error), "
            "stopped_at = COALESCE(?, stopped_at) WHERE worker_id = ?"
        )
        params: List[Any] = [
            status.value,
            str(current_task_id) if current_task_id else None,
            error,
            stopped_at,
            worker_id,
        ]
        if only_if_live:
            query += f" AND status IN ({','.join('?' for _ in LIVE_WORKER_STATUSES)})"
            params.extend(s.value for s in LIVE_WORKER_STATUSES)
        with self._db.immediate_transaction() as conn:
            return conn.execute(query, params).rowcount == 1

    @staticmethod
    def record_task_outcome(conn: sqlite3.Connection, worker_id: str, success: bool) -> None:
        """Increment the worker's completion counters inside an existing transaction."""
        column = "tasks_completed" if success else "tasks_failed"
        conn.execute(f"UPDATE workers SET {column} = {column} + 1 WHERE worker_id = ?", (worker_id,))

    def get(self, worker_id: str) -> Optional[WorkerInfo]:
        with self._db.transaction() as conn:
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return self._row_to_info(row) if row else None

    def list(self, status: Optional[WorkerStatus] = None, limit: int = 500) -> List[WorkerInfo]:
        query = "SELECT * FROM workers"
        params: List[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status.value)
        query += f" ORDER BY started_at DESC LIMIT {int(limit)}"
        with self._db.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_info(r) for r in rows]

    def clear(self) -> None:
        """Delete all worker rows (and, by cascade, their leases); intended for testing."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM workers")

    @staticmethod
    def _row_to_info(row: sqlite3.Row) -> WorkerInfo:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        return WorkerInfo(
            worker_id=row["worker_id"],
            status=WorkerStatus(row["status"]),
            current_task_id=TaskId(id=UUID(row["current_task_id"])) if row["current_task_id"] else None,
            tasks_completed=row["tasks_completed"],
            tasks_failed=row["tasks_failed"],
            started_at=datetime.fromisoformat(row["started_at"]),
            last_heartbeat_at=datetime.fromisoformat(row["last_heartbeat_at"]),
            last_error=row["last_error"],
            metadata=metadata,
            stopped_at=parse_ts(row["stopped_at"]),
        )
