"""Database connection and lifecycle management for BABY.

Provides durable SQLite connection management with:
- WAL (Write-Ahead Logging) journal mode for concurrent read/write access
- Busy timeout to prevent lock contention
- Automatic schema initialization with idempotent migrations
- Thread-safe transaction boundaries with rollback guarantees
- Safe handling of file-based, URI-based, and in-memory databases
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

            self._initialized = True
            logger.info("Database schema initialized successfully")

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


# Default database manager singleton
db_manager = DatabaseManager()
