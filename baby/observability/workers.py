"""Worker operational visibility and state tracking for BABY.

Provides read-only operational visibility into background/pool workers,
including lifecycle status, heartbeats, assigned tasks, and completion counts.
"""

import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

from baby.core import TaskId, WorkerInfo, WorkerStatus
from baby.observability.metrics import metrics_collector


class WorkerTracker:
    """Thread-safe worker tracking registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._workers: Dict[str, WorkerInfo] = {}

    def register_worker(
        self,
        worker_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        status: WorkerStatus = WorkerStatus.IDLE,
    ) -> WorkerInfo:
        """Register or re-register a worker."""
        with self._lock:
            now = datetime.utcnow()
            info = WorkerInfo(
                worker_id=worker_id,
                status=status,
                started_at=now,
                last_heartbeat_at=now,
                metadata=metadata or {},
            )
            self._workers[worker_id] = info
            self._update_worker_metrics()
            return info

    def record_heartbeat(self, worker_id: str) -> Optional[WorkerInfo]:
        """Record a heartbeat timestamp for an active worker."""
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker:
                worker.last_heartbeat_at = datetime.utcnow()
            return worker

    def assign_task(self, worker_id: str, task_id: TaskId) -> Optional[WorkerInfo]:
        """Mark worker as busy executing a task."""
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker:
                worker.status = WorkerStatus.BUSY
                worker.current_task_id = task_id
                worker.last_heartbeat_at = datetime.utcnow()
                self._update_worker_metrics()
            return worker

    def complete_task(self, worker_id: str, success: bool, error: Optional[str] = None) -> Optional[WorkerInfo]:
        """Record task completion for a worker and return it to idle."""
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker:
                worker.status = WorkerStatus.IDLE
                worker.current_task_id = None
                worker.last_heartbeat_at = datetime.utcnow()
                if success:
                    worker.tasks_completed += 1
                    metrics_collector.increment_counter("worker_tasks_completed_total", labels={"worker_id": worker_id})
                else:
                    worker.tasks_failed += 1
                    worker.last_error = error
                    metrics_collector.increment_counter("worker_tasks_failed_total", labels={"worker_id": worker_id})
                self._update_worker_metrics()
            return worker

    def set_status(self, worker_id: str, status: WorkerStatus, error: Optional[str] = None) -> Optional[WorkerInfo]:
        """Update worker operational status."""
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker:
                worker.status = status
                worker.last_heartbeat_at = datetime.utcnow()
                if error:
                    worker.last_error = error
                self._update_worker_metrics()
            return worker

    def get_worker(self, worker_id: str) -> Optional[WorkerInfo]:
        """Retrieve operational info for a worker."""
        with self._lock:
            return self._workers.get(worker_id)

    def list_workers(self, status: Optional[WorkerStatus] = None) -> List[WorkerInfo]:
        """List workers, optionally filtered by status."""
        with self._lock:
            workers = list(self._workers.values())
            if status is not None:
                workers = [w for w in workers if w.status == status]
            return workers

    def unregister_worker(self, worker_id: str) -> bool:
        """Remove a worker from tracking."""
        with self._lock:
            removed = self._workers.pop(worker_id, None) is not None
            if removed:
                self._update_worker_metrics()
            return removed

    def _update_worker_metrics(self) -> None:
        """Update worker gauges in metrics_collector."""
        total = len(self._workers)
        active = sum(1 for w in self._workers.values() if w.status in (WorkerStatus.IDLE, WorkerStatus.BUSY))
        busy = sum(1 for w in self._workers.values() if w.status == WorkerStatus.BUSY)
        metrics_collector.set_gauge("workers_total", float(total))
        metrics_collector.set_gauge("workers_active", float(active))
        metrics_collector.set_gauge("workers_busy", float(busy))

    def clear(self) -> None:
        """Clear all worker records; intended for testing."""
        with self._lock:
            self._workers.clear()
            self._update_worker_metrics()


# Global worker tracker instance
worker_tracker = WorkerTracker()
