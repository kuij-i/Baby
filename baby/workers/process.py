"""Entry point executed inside each isolated worker OS process.

Workers are always started with the ``spawn`` multiprocessing start method — the only
method available on Windows and the safest on Linux/macOS (no inherited locks, sockets,
SQLite connections or API state). ``run_worker_process`` is a top-level, importable
function so it can be pickled by reference; it never starts other workers
(``BABY_WORKER_PROCESS`` guards against recursive spawning).
"""

import asyncio
import os
import signal
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from baby.workers.bootstrap import DEFAULT_BOOTSTRAP

WORKER_PROCESS_ENV = "BABY_WORKER_PROCESS"

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_CONFIG = 2

# Non-secret settings a supervisor may forward to its workers. Secrets (API keys,
# tokens) are never passed through process arguments; workers read them from the
# environment like any other process.
FORWARDABLE_SETTINGS = frozenset(
    {
        "persistence_backend",
        "audit_storage_type",
        "coding_repo_root",
        "coding_max_iterations",
        "coding_max_tool_calls_per_iteration",
        "coding_max_tool_retries",
        "worker_poll_interval_seconds",
        "worker_heartbeat_interval_seconds",
        "worker_stale_timeout_seconds",
        "task_lease_duration_seconds",
        "worker_max_task_seconds",
        "approval_ttl_seconds",
        "max_approval_rounds",
        "log_level",
        "model_provider",
        "default_model",
        "openai_base_url",
        "nvidia_base_url",
        "nvidia_model",
    }
)


@dataclass(frozen=True)
class WorkerProcessConfig:
    """Picklable configuration handed from the supervisor to one worker process."""

    worker_id: str
    database_url: str
    slot: int = 0
    bootstrap: str = DEFAULT_BOOTSTRAP
    settings_overrides: Dict[str, Any] = field(default_factory=dict)
    max_tasks: Optional[int] = None


def in_worker_process() -> bool:
    return os.environ.get(WORKER_PROCESS_ENV) == "1"


def run_worker_process(config: WorkerProcessConfig, stop_event: Any = None) -> None:
    """Initialize an isolated worker runtime and run it until stopped. Exits the process."""
    os.environ[WORKER_PROCESS_ENV] = "1"
    sys.exit(_run(config, stop_event))


def _install_signal_handlers(stop_event: Any) -> None:
    if stop_event is None:
        return

    def _handler(signum: int, frame: Any) -> None:
        stop_event.set()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass


def _run(config: WorkerProcessConfig, stop_event: Any) -> int:
    from baby.audit import audit_log
    from baby.configuration import settings, validate_settings
    from baby.errors import ConfigurationError
    from baby.logging import configure_logging, get_logger
    from baby.persistence.database import db_manager
    from baby.persistence.sqlite_audit import SQLiteAuditLog
    from baby.workers.bootstrap import resolve_bootstrap
    from baby.workers.runtime import WorkerRuntime, WorkerRuntimeConfig

    logger = get_logger("baby.workers.process")
    _install_signal_handlers(stop_event)

    try:
        # Configuration must be valid before any state is touched (fail closed).
        for key, value in config.settings_overrides.items():
            if key not in FORWARDABLE_SETTINGS:
                raise ConfigurationError(f"Setting '{key}' cannot be forwarded to worker processes")
            setattr(settings, key, value)
        configure_logging(settings.log_level)
        validate_settings(settings)
        if settings.persistence_backend != "sqlite":
            raise ConfigurationError("Worker processes require persistence_backend='sqlite'")

        db_manager.configure(config.database_url)
        if db_manager.is_memory:
            raise ConfigurationError("Worker processes require a file-backed SQLite database, not :memory:")
        db_manager.init_schema()
        if settings.audit_storage_type == "sqlite":
            audit_log.use_backend(SQLiteAuditLog(db_manager))

        resolve_bootstrap(config.bootstrap)()

        runtime = WorkerRuntime(
            WorkerRuntimeConfig.from_settings(worker_id=config.worker_id, slot=config.slot, max_tasks=config.max_tasks),
            stop_signal=stop_event,
        )
        return asyncio.run(runtime.run())
    except ConfigurationError as exc:
        logger.error("Worker configuration invalid; refusing to start", error=str(exc))
        return EXIT_CONFIG
    except Exception as exc:
        logger.error("Worker process failed", error=str(exc), worker_id=config.worker_id)
        return EXIT_FATAL
    finally:
        try:
            db_manager.close()
        except Exception:
            pass
