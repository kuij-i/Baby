"""Database connection and lifecycle management for BABY.

Provides durable SQLite connection management with:
- WAL (Write-Ahead Logging) journal mode for concurrent read/write access
- Busy timeout to prevent lock contention
- Automatic schema initialization with idempotent migrations
- Thread-safe transaction boundaries with rollback guarantees
- Safe handling of file-based, URI-based, and in-memory databases
- ``immediate_transaction()`` (BEGIN IMMEDIATE) for cross-process read-modify-write
  operations such as durable task claiming

Schema versioning: ``init_schema()`` is idempotent and records the schema version in
``PRAGMA user_version``. Version 2 (Phase 12) adds the ``workers`` and ``task_leases``
tables and the ``task_records.cancel_requested`` / ``task_records.claim_count`` columns.
Existing version-1 databases are upgraded in place with ``ALTER TABLE ... ADD COLUMN``
(defaults keep existing rows valid); no data is rewritten or removed. Version 3 (Phase 17)
adds the ``task_idempotency_keys`` table (``CREATE TABLE IF NOT EXISTS``, additive only).
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional
from urllib.parse import urlparse

from baby.errors import ConfigurationError
from baby.logging import get_logger

logger = get_logger(__name__)

SCHEMA_VERSION = 3


def parse_sqlite_path(database_url: str) -> str:
    """Extract clean filesystem path or :memory: from a database URL.

    Supported formats:
    - 'sqlite:///./baby.db' -> './baby.db'
    - 'sqlite:////absolute/path/baby.db' -> '/absolute/path/baby.db'
    - 'sqlite:///:memory:' -> ':memory:'
    - ':memory:' -> ':memory:'
    - './baby.db' -> './baby.db'
    """
    if not database_url or not database_url.strip():
        raise ConfigurationError("Database URL cannot be empty")

    url = database_url.strip()
    if url in (":memory:", "sqlite:///:memory:"):
        return ":memory:"

    if url.startswith("sqlite:///"):
        path_str = url[10:]
        if not path_str:
            raise ConfigurationError(f"Invalid sqlite database URL (empty path): {database_url}")
        return path_str

    if url.startswith("sqlite://"):
        parsed = urlparse(url)
        path_str = parsed.path
        if not path_str:
            raise ConfigurationError(f"Invalid sqlite database URL: {database_url}")
        return path_str

    if url.startswith("sqlite:"):
        path_str = url[7:]
        if not path_str:
            raise ConfigurationError(f"Invalid sqlite database URL: {database_url}")
        return path_str

    return url


class DatabaseManager:
    """Thread-safe SQLite database manager for BABY persistence."""

    def __init__(self, database_url: str = "sqlite:///./baby.db") -> None:
        self._database_url = database_url
        self._db_path = parse_sqlite_path(database_url)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._initialized = False

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def database_url(self) -> str:
        return self._database_url

    def configure(self, database_url: str) -> None:
        """Point this manager at a different database, closing any open connection.

        Used by worker processes to bind the process-wide singleton to the database
        their supervisor assigned, before any task work starts.
        """
        new_path = parse_sqlite_path(database_url)
        with self._lock:
            self.close()
            self._database_url = database_url
            self._db_path = new_path

    @property
    def is_memory(self) -> bool:
        return self._db_path == ":memory:"

    def get_connection(self) -> sqlite3.Connection:
        """Return the active SQLite connection, creating it if needed."""
        with self._lock:
            if self._conn is None:
                # Ensure directory exists if file-based
                if not self.is_memory:
                    parent_dir = Path(self._db_path).parent
                    if not parent_dir.exists():
                        try:
                            parent_dir.mkdir(parents=True, exist_ok=True)
                        except Exception as exc:
                            raise ConfigurationError(
                                f"Failed to create database directory {parent_dir}: {exc}"
                            ) from exc

                try:
                    conn = sqlite3.connect(
                        self._db_path,
                        check_same_thread=False,
                        timeout=10.0,
                        detect_types=sqlite3.PARSE_DECLTYPES,
                    )
                    conn.row_factory = sqlite3.Row

                    # Enable WAL mode and busy timeout for concurrency
                    if not self.is_memory:
                        conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("PRAGMA synchronous=NORMAL;")
                    conn.execute("PRAGMA busy_timeout=5000;")
                    conn.execute("PRAGMA foreign_keys=ON;")
                except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
                    raise ConfigurationError(
                        f"Failed to initialize SQLite database at '{self._db_path}': {exc}"
                    ) from exc

                self._conn = conn
                logger.info(
                    "SQLite connection established",
                    path=":memory:" if self.is_memory else str(self._db_path),
                    wal=not self.is_memory,
                )

            return self._conn

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager providing thread-safe, transactional SQLite execution.

        Automatically commits on successful block exit, or rolls back on exception.
        """
        with self._lock:
            conn = self.get_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @contextmanager
    def immediate_transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Transaction that takes the SQLite write lock up front (``BEGIN IMMEDIATE``).

        Use for every read-modify-write that other processes may race on (claiming,
        lease renewal, status transitions). Concurrent writers in other processes wait
        up to ``busy_timeout`` instead of failing on a stale read snapshot, and two
        writers can never both observe the same pre-image.
        """
        with self._lock:
            conn = self.get_connection()
            if conn.in_transaction:
                raise RuntimeError("immediate_transaction() cannot be nested inside another transaction")
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def init_schema(self) -> None:
        """Initialize all required database tables and indices idempotently."""
        with self._lock:
            with self.transaction() as conn:
                # 1. Memory Records table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS memory_records (
                        id TEXT PRIMARY KEY,
                        task_id TEXT,
                        agent_id TEXT,
                        record_type TEXT NOT NULL,
                        content TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT
                    );
                    """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_task_id ON memory_records(task_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_agent_id ON memory_records(agent_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_type ON memory_records(record_type);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_created_at ON memory_records(created_at);")

                # 2. Audit Events table
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS audit_events (
                        id TEXT PRIMARY KEY,
                        event_type TEXT NOT NULL,
                        task_id TEXT,
                        agent_id TEXT,
                        user_id TEXT,
                        timestamp TEXT NOT NULL,
                        details TEXT NOT NULL
                    );
                    """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_task_id ON audit_events(task_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_agent_id ON audit_events(agent_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_events(event_type);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_user_id ON audit_events(user_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_events(timestamp);")

                # 3. Task Records table (state machine backing)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS task_records (
                        task_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        description TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        completed_at TEXT,
                        user_id TEXT,
                        priority INTEGER NOT NULL DEFAULT 0,
                        assigned_agent_id TEXT,
                        assigned_worker_id TEXT,
                        plan TEXT,
                        result TEXT,
                        error TEXT,
                        approvals TEXT,
                        verifications TEXT,
                        retry_count INTEGER NOT NULL DEFAULT 0
                    );
                    """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON task_records(status);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON task_records(user_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON task_records(created_at);")

                # Schema v2 (Phase 12): durable worker ownership and leases
                self._add_column_if_missing(
                    conn,
                    "task_records",
                    "cancel_requested",
                    "INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1))",
                )
                self._add_column_if_missing(
                    conn, "task_records", "claim_count", "INTEGER NOT NULL DEFAULT 0 CHECK (claim_count >= 0)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_claim_queue ON task_records(status, priority, created_at);"
                )

                # 4. Workers: one row per worker process identity (never reused)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS workers (
                        worker_id TEXT PRIMARY KEY,
                        hostname TEXT NOT NULL,
                        pid INTEGER NOT NULL,
                        status TEXT NOT NULL CHECK (status IN (
                            'starting', 'idle', 'busy', 'paused', 'stopping', 'stopped', 'stale', 'error'
                        )),
                        started_at TEXT NOT NULL,
                        last_heartbeat_at TEXT NOT NULL,
                        stopped_at TEXT,
                        current_task_id TEXT REFERENCES task_records(task_id) ON DELETE SET NULL,
                        tasks_completed INTEGER NOT NULL DEFAULT 0 CHECK (tasks_completed >= 0),
                        tasks_failed INTEGER NOT NULL DEFAULT 0 CHECK (tasks_failed >= 0),
                        last_error TEXT,
                        metadata TEXT NOT NULL DEFAULT '{}'
                    );
                    """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_workers_status_heartbeat ON workers(status, last_heartbeat_at);"
                )

                # 5. Task leases: at most one live claim per task (PRIMARY KEY on task_id)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS task_leases (
                        task_id TEXT PRIMARY KEY REFERENCES task_records(task_id) ON DELETE CASCADE,
                        worker_id TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE CASCADE,
                        fence INTEGER NOT NULL CHECK (fence > 0),
                        claimed_at TEXT NOT NULL,
                        renewed_at TEXT NOT NULL,
                        lease_expires_at TEXT NOT NULL
                    );
                    """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_task_leases_worker ON task_leases(worker_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_task_leases_expires ON task_leases(lease_expires_at);")

                # Schema v3 (Phase 17): idempotent task submission. One row per
                # (principal, engagement, key); written in the same transaction as the task
                # it points to, so a key can never be reserved without its task.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS task_idempotency_keys (
                        principal TEXT NOT NULL,
                        engagement_id TEXT NOT NULL,
                        key_hash TEXT NOT NULL,
                        request_fingerprint TEXT NOT NULL,
                        task_id TEXT NOT NULL REFERENCES task_records(task_id) ON DELETE CASCADE,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (principal, engagement_id, key_hash)
                    );
                    """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_task_idempotency_task ON task_idempotency_keys(task_id);")

                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")

            self._initialized = True
            logger.info("Database schema initialized successfully")

    @staticmethod
    def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        """Idempotent in-place migration helper (SQLite has no ADD COLUMN IF NOT EXISTS)."""
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table});").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl};")
            logger.info("Database schema migrated", table=table, column=column)

    def schema_version(self) -> int:
        """Return the schema version recorded in ``PRAGMA user_version``."""
        with self.transaction() as conn:
            row = conn.execute("PRAGMA user_version;").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        """Close connection and reset state."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception as exc:
                    logger.warning("Error closing SQLite connection", error=str(exc))
                finally:
                    self._conn = None
                    self._initialized = False


def _default_database_url() -> str:
    from baby.configuration import settings

    return settings.database_url or "sqlite:///./baby.db"


# Default database manager singleton (honors settings.database_url)
db_manager = DatabaseManager(_default_database_url())
