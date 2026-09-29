"""Durable SQLite implementation of task record persistence."""

import json
import sqlite3
from datetime import datetime
from typing import Any, List, Optional
from uuid import UUID

from baby.core import (
    AgentId,
    AgentResult,
    ApprovalRequest,
    Plan,
    TaskId,
    TaskRecord,
    TaskStatus,
    VerificationResult,
)
from baby.persistence.database import DatabaseManager, db_manager


class SQLiteTaskStore:
    """Durable SQLite storage for TaskRecord models.

    Ensures task operational records, plan steps, approvals, and outcomes
    persist across restarts while keeping Python domain state-machine authoritative.
    """

    def __init__(self, manager: Optional[DatabaseManager] = None) -> None:
        self._db = manager or db_manager
        self._db.init_schema()

    def save_task(self, record: TaskRecord) -> None:
        """Persist or update a task record in SQLite."""
        task_id_str = str(record.task_id)
        created_at_str = record.created_at.isoformat()
        updated_at_str = record.updated_at.isoformat()
        completed_at_str = record.completed_at.isoformat() if record.completed_at else None
        agent_id_str = str(record.assigned_agent_id) if record.assigned_agent_id else None

        plan_json = json.dumps(record.plan.model_dump(mode="json")) if record.plan else None
        result_json = json.dumps(record.result.model_dump(mode="json")) if record.result else None
        approvals_json = json.dumps([a.model_dump(mode="json") for a in record.approvals])
        verifications_json = json.dumps([v.model_dump(mode="json") for v in record.verifications])

        with self._db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO task_records (
                    task_id, title, description, status, created_at, updated_at, completed_at,
                    user_id, priority, assigned_agent_id, assigned_worker_id, plan, result,
                    error, approvals, verifications, retry_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    title=excluded.title,
                    description=excluded.description,
                    status=excluded.status,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    completed_at=excluded.completed_at,
                    user_id=excluded.user_id,
                    priority=excluded.priority,
                    assigned_agent_id=excluded.assigned_agent_id,
                    assigned_worker_id=excluded.assigned_worker_id,
                    plan=excluded.plan,
                    result=excluded.result,
                    error=excluded.error,
                    approvals=excluded.approvals,
                    verifications=excluded.verifications,
                    retry_count=excluded.retry_count;
                """,
                (
                    task_id_str,
                    record.title,
                    record.description,
                    record.status.value,
                    created_at_str,
                    updated_at_str,
                    completed_at_str,
                    record.user_id,
                    record.priority,
                    agent_id_str,
                    record.assigned_worker_id,
                    plan_json,
                    result_json,
                    record.error,
                    approvals_json,
                    verifications_json,
                    getattr(record, "retry_count", 0),
                ),
            )

    def get_task(self, task_id: TaskId) -> Optional[TaskRecord]:
        """Retrieve task record by TaskId."""
        with self._db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM task_records WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()

        if row is None:
            return None

        return self._row_to_record(row)

    def list_tasks(
        self,
        user_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[TaskRecord]:
        """List tasks with optional engagement and status filters and pagination."""
        query = "SELECT * FROM task_records WHERE 1=1"
        params: List[Any] = []

        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)

        if status is not None:
            query += " AND status = ?"
            params.append(status.value)

        query += " ORDER BY created_at DESC"

        if limit >= 0:
            query += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        elif offset > 0:
            query += f" LIMIT -1 OFFSET {int(offset)}"

        with self._db.transaction() as conn:
            rows = conn.execute(query, params).fetchall()

        return [self._row_to_record(row) for row in rows]

    def count_tasks(
        self,
        user_id: Optional[str] = None,
        status: Optional[TaskStatus] = None,
    ) -> int:
        """Count tasks matching the filters."""
        query = "SELECT COUNT(*) as count FROM task_records WHERE 1=1"
        params: List[Any] = []

        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)

        if status is not None:
            query += " AND status = ?"
            params.append(status.value)

        with self._db.transaction() as conn:
            row = conn.execute(query, params).fetchone()
            return int(row["count"]) if row else 0

    def clear(self) -> None:
        """Clear all tasks; intended for testing."""
        with self._db.transaction() as conn:
            conn.execute("DELETE FROM task_records")

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> TaskRecord:
        plan_obj = Plan.model_validate(json.loads(row["plan"])) if row["plan"] else None
        result_obj = AgentResult.model_validate(json.loads(row["result"])) if row["result"] else None

        approvals_list = []
        if row["approvals"]:
            approvals_data = json.loads(row["approvals"])
            approvals_list = [ApprovalRequest.model_validate(a) for a in approvals_data]

        verifications_list = []
        if row["verifications"]:
            verifications_data = json.loads(row["verifications"])
            verifications_list = [VerificationResult.model_validate(v) for v in verifications_data]

        created_at = datetime.fromisoformat(row["created_at"])
        updated_at = datetime.fromisoformat(row["updated_at"])
        completed_at = datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None

        agent_id = AgentId(id=row["assigned_agent_id"]) if row["assigned_agent_id"] else None

        return TaskRecord(
            task_id=TaskId(id=UUID(row["task_id"])),
            title=row["title"],
            description=row["description"],
            status=TaskStatus(row["status"]),
            created_at=created_at,
            updated_at=updated_at,
            completed_at=completed_at,
            user_id=row["user_id"],
            priority=row["priority"],
            assigned_agent_id=agent_id,
            assigned_worker_id=row["assigned_worker_id"],
            plan=plan_obj,
            result=result_obj,
            error=row["error"],
            approvals=approvals_list,
            verifications=verifications_list,
        )
