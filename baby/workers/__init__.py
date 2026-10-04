"""Durable, process-isolated task workers for BABY.

Tasks are submitted durably (PENDING rows in SQLite), claimed atomically by worker
processes holding fenced leases, and executed outside the API process. See CLAUDE.md
("Durable workers") for the lifecycle, lease, heartbeat and recovery semantics.

Run workers with ``python -m baby.workers run``.
"""

from baby.workers.bootstrap import DEFAULT_BOOTSTRAP, default_bootstrap, resolve_bootstrap
from baby.workers.queue import ClaimedTask, DurableTaskQueue, LeaseState, approvals_ready
from baby.workers.recovery import RecoveryReport, WorkerRecovery
from baby.workers.runtime import WorkerRuntime, WorkerRuntimeConfig, new_worker_id

__all__ = [
    "DEFAULT_BOOTSTRAP",
    "ClaimedTask",
    "DurableTaskQueue",
    "LeaseState",
    "RecoveryReport",
    "WorkerRecovery",
    "WorkerRuntime",
    "WorkerRuntimeConfig",
    "approvals_ready",
    "default_bootstrap",
    "new_worker_id",
    "resolve_bootstrap",
]
