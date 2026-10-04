"""Durable persistence layer for BABY.

Provides thread-safe SQLite persistence for:
- MemoryStore (SQLiteMemoryStore)
- AuditLog (SQLiteAuditLog)
- Task records (SQLiteTaskStore)
- Worker registry (SQLiteWorkerStore)
"""

from baby.persistence.database import DatabaseManager, db_manager, parse_sqlite_path
from baby.persistence.sqlite_audit import SQLiteAuditLog
from baby.persistence.sqlite_memory import SQLiteMemoryStore
from baby.persistence.sqlite_tasks import SQLiteTaskStore
from baby.persistence.sqlite_workers import SQLiteWorkerStore

__all__ = [
    "DatabaseManager",
    "SQLiteAuditLog",
    "SQLiteMemoryStore",
    "SQLiteTaskStore",
    "SQLiteWorkerStore",
    "db_manager",
    "parse_sqlite_path",
]
