"""Command-line entry point for durable workers.

    python -m baby.workers run [--count N] [--bootstrap module:function]
    python -m baby.workers recover
    python -m baby.workers status

The ``if __name__ == "__main__"`` guard is required: with the ``spawn`` start method
(Windows, and always used by Baby) each child re-imports the parent's main module, and
the guard prevents children from starting supervisors of their own.
"""

import argparse
import signal
import sys
import threading
from typing import List, Optional


def _cmd_run(args: argparse.Namespace) -> int:
    from baby.configuration import settings
    from baby.logging import configure_logging
    from baby.workers.supervisor import WorkerSupervisor

    configure_logging(settings.log_level)
    stop = threading.Event()

    def _handler(signum: int, frame: object) -> None:
        stop.set()

    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, _handler)

    supervisor = WorkerSupervisor(worker_count=args.count, bootstrap=args.bootstrap)
    supervisor.start()
    try:
        while not stop.wait(settings.worker_heartbeat_interval_seconds):
            supervisor.monitor_once()
    finally:
        supervisor.stop()
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:
    from baby.observability.tasks import task_tracker
    from baby.persistence.database import db_manager
    from baby.workers.recovery import WorkerRecovery

    db_manager.init_schema()
    report = WorkerRecovery().recover()
    orphans = task_tracker.recover_stale_tasks()
    print(
        f"stale_workers={len(report.stale_workers)} "
        f"failed_leased_tasks={len(report.failed_tasks)} failed_orphan_tasks={len(orphans)}"
    )
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    from baby.persistence.sqlite_workers import SQLiteWorkerStore

    for worker in SQLiteWorkerStore().list():
        print(
            f"{worker.worker_id}  {worker.status.value:<9} heartbeat={worker.last_heartbeat_at.isoformat()} "
            f"task={worker.current_task_id or '-'} done={worker.tasks_completed} failed={worker.tasks_failed}"
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    from baby.errors import ConfigurationError
    from baby.workers.bootstrap import DEFAULT_BOOTSTRAP

    parser = argparse.ArgumentParser(prog="python -m baby.workers", description="Baby durable worker processes")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="start and supervise worker processes")
    run.add_argument("--count", type=int, default=None, help="number of worker processes (default: worker_count)")
    run.add_argument("--bootstrap", default=DEFAULT_BOOTSTRAP, help="module:function registering agents")
    sub.add_parser("recover", help="fail closed any task whose worker or lease was lost")
    sub.add_parser("status", help="list registered workers")
    args = parser.parse_args(argv)

    handlers = {"run": _cmd_run, "recover": _cmd_recover, "status": _cmd_status}
    try:
        return handlers[args.command](args)
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
