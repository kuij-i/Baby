"""Minimal supervisor for durable worker processes.

Responsibilities (and nothing more):
    - start ``worker_count`` isolated worker processes (``spawn`` start method);
    - monitor them: a process that exited is recovered immediately (its leases revoked,
      its in-progress task failed closed) and, unless stopping, replaced by a new worker
      with a new identity;
    - periodically run stale-worker recovery (covers hung processes and workers on other hosts);
    - graceful shutdown: signal every worker, wait ``worker_shutdown_grace_seconds``,
      terminate stragglers, then recover anything they left behind.

Exclusive task ownership is never the supervisor's job: it is enforced by the database
(atomic claims and fenced leases), so even a misbehaving supervisor cannot cause a task to
run twice.
"""

import multiprocessing
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from baby.errors import ConfigurationError
from baby.logging import get_logger
from baby.workers.bootstrap import DEFAULT_BOOTSTRAP
from baby.workers.process import (
    EXIT_CONFIG,
    FORWARDABLE_SETTINGS,
    WorkerProcessConfig,
    in_worker_process,
    run_worker_process,
)
from baby.workers.recovery import REASON_WORKER_EXITED, WorkerRecovery
from baby.workers.runtime import new_worker_id

logger = get_logger(__name__)


@dataclass
class _WorkerHandle:
    slot: int
    worker_id: str
    process: Any  # multiprocessing.context.SpawnProcess
    stop_event: Any
    recovered: bool = False


def absolute_database_url(db_path: str) -> str:
    """Absolute ``sqlite:///`` URL so children resolve the same file regardless of cwd."""
    return "sqlite:///" + str(Path(db_path).resolve())


class WorkerSupervisor:
    """Starts, monitors and stops isolated worker processes."""

    def __init__(
        self,
        worker_count: Optional[int] = None,
        database_url: Optional[str] = None,
        bootstrap: str = DEFAULT_BOOTSTRAP,
        restart: bool = True,
        max_restarts: int = 10,
        max_tasks_per_worker: Optional[int] = None,
        settings_overrides: Optional[Dict[str, Any]] = None,
    ) -> None:
        from baby.configuration import settings

        self._count = worker_count if worker_count is not None else settings.worker_count
        self._database_url = database_url
        self._bootstrap = bootstrap
        self._restart = restart
        self._max_restarts = max_restarts
        self._max_tasks = max_tasks_per_worker
        self._overrides = settings_overrides
        self._ctx = multiprocessing.get_context("spawn")
        self._handles: List[_WorkerHandle] = []
        self._restarts = 0
        self._stopping = False
        self._lock = threading.RLock()
        self._monitor_stop = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._recovery: Optional[WorkerRecovery] = None

    @property
    def workers(self) -> List[_WorkerHandle]:
        with self._lock:
            return list(self._handles)

    @property
    def restarts(self) -> int:
        return self._restarts

    def _forwarded_settings(self) -> Dict[str, Any]:
        from baby.configuration import settings

        values = {key: getattr(settings, key) for key in FORWARDABLE_SETTINGS}
        values.update(self._overrides or {})
        return values

    def start(self) -> None:
        """Validate configuration, recover stale state, then spawn the workers."""
        from baby.configuration import settings, validate_settings
        from baby.persistence.database import db_manager

        if in_worker_process():
            raise RuntimeError("Worker processes must not start worker supervisors (recursive spawn)")
        validate_settings(settings)
        if settings.persistence_backend != "sqlite":
            raise ConfigurationError("Workers require persistence_backend='sqlite'")
        if self._database_url is None:
            if db_manager.is_memory:
                raise ConfigurationError("Workers require a file-backed SQLite database, not :memory:")
            self._database_url = absolute_database_url(db_manager.db_path)
        if self._bootstrap == DEFAULT_BOOTSTRAP:
            # Fail before spawning anything: each worker would refuse to start (config exit, never
            # restarted) with the same error, leaving the supervisor running with no workers.
            from baby.persistence.database import parse_sqlite_path
            from baby.tools.coding.path_resolver import validate_coding_repo_root

            validate_coding_repo_root(
                repo_root=self._forwarded_settings().get("coding_repo_root"),
                database_path=parse_sqlite_path(self._database_url),
            )
        db_manager.init_schema()
        self._recovery = WorkerRecovery()
        self._recovery.recover()

        with self._lock:
            self._stopping = False
            for slot in range(self._count):
                self._spawn(slot)
        logger.info("Worker supervisor started", workers=self._count)

    def _spawn(self, slot: int) -> _WorkerHandle:
        assert self._database_url is not None
        worker_id = new_worker_id()
        stop_event = self._ctx.Event()
        config = WorkerProcessConfig(
            worker_id=worker_id,
            database_url=self._database_url,
            slot=slot,
            bootstrap=self._bootstrap,
            settings_overrides=self._forwarded_settings(),
            max_tasks=self._max_tasks,
        )
        process = self._ctx.Process(
            target=run_worker_process,
            args=(config, stop_event),
            name=f"baby-worker-{slot}",
            daemon=False,
        )
        process.start()
        handle = _WorkerHandle(slot=slot, worker_id=worker_id, process=process, stop_event=stop_event)
        self._handles.append(handle)
        logger.info("Worker process spawned", worker_id=worker_id, pid=process.pid, slot=slot)
        return handle

    def monitor_once(self) -> List[str]:
        """Detect exited workers, recover them, restart if allowed, and run stale recovery.

        Returns the worker IDs recovered in this pass.
        """
        recovered: List[str] = []
        recovery = self._recovery or WorkerRecovery()
        with self._lock:
            for handle in list(self._handles):
                if handle.recovered or handle.process.is_alive():
                    continue
                exitcode = handle.process.exitcode
                recovery.recover_worker(
                    handle.worker_id,
                    reason=REASON_WORKER_EXITED,
                    error=f"Worker process exited with code {exitcode}",
                )
                handle.recovered = True
                recovered.append(handle.worker_id)
                # Exiting with 0 while the supervisor is stopping is the expected, graceful path.
                log = logger.info if self._stopping and exitcode == 0 else logger.warning
                log("Worker process exited", worker_id=handle.worker_id, exitcode=exitcode)
                clean_exit = exitcode == 0 and self._max_tasks is not None
                # A worker that refused to start because of invalid configuration would fail
                # identically on every restart; do not restart-loop it.
                restartable = not clean_exit and exitcode != EXIT_CONFIG
                if not self._stopping and self._restart and restartable and self._restarts < self._max_restarts:
                    self._restarts += 1
                    self._spawn(handle.slot)
        recovery.recover()
        return recovered

    def start_monitor_thread(self, interval: Optional[float] = None) -> None:
        """Run ``monitor_once`` periodically in a background thread (used by the API lifespan)."""
        from baby.configuration import settings

        period = interval if interval is not None else settings.worker_heartbeat_interval_seconds

        def _loop() -> None:
            while not self._monitor_stop.wait(period):
                try:
                    self.monitor_once()
                except Exception as exc:
                    logger.error("Worker supervisor monitor pass failed", error=str(exc))

        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(target=_loop, name="baby-worker-supervisor", daemon=True)
        self._monitor_thread.start()

    def stop(self, timeout: Optional[float] = None) -> None:
        """Gracefully stop every worker; terminate any that do not exit within ``timeout``."""
        from baby.configuration import settings

        grace = settings.worker_shutdown_grace_seconds if timeout is None else timeout
        self._monitor_stop.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=5)
        with self._lock:
            self._stopping = True
            for handle in self._handles:
                handle.stop_event.set()
            for handle in self._handles:
                handle.process.join(timeout=grace)
            for handle in self._handles:
                if handle.process.is_alive():
                    logger.warning("Worker did not stop within grace period; terminating", worker_id=handle.worker_id)
                    handle.process.terminate()
                    handle.process.join(timeout=5)
                    if handle.process.is_alive():
                        handle.process.kill()
                        handle.process.join(timeout=5)
        self.monitor_once()
        logger.info("Worker supervisor stopped")
