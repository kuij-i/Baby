"""BABY FastAPI observability application.

Provides read-only operational endpoints for health, metrics, tasks,
workers, agents, and audit visibility. All endpoints are protected by
the existing authentication and engagement-scoping model.

Phase 10 additions:
- FastAPI lifespan handler for deterministic startup/shutdown
- Startup: validates configuration, recovers stale tasks
- Shutdown: stops workers cleanly, logs lifecycle events
"""

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from baby.api.middleware import ReadOnlyMiddleware
from baby.api.routes import agents, audit, health, metrics, readiness, tasks, workers
from baby.logging import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: deterministic startup and shutdown.

    Startup:
        1. Validate critical configuration
        2. Recover stale in-progress tasks (fail-closed restart recovery)
        3. Log readiness

    Shutdown:
        1. Log shutdown signal
        2. Flush observability state (in-memory; no durable flush needed at this phase)
        3. Log clean shutdown
    """
    # ---- STARTUP ----
    logger.info("BABY observability API starting up")

    # Import here to avoid circular imports at module load time
    from baby.configuration import settings
    from baby.core import WorkerStatus
    from baby.observability.tasks import task_tracker
    from baby.observability.workers import worker_tracker

    # Validate configuration at startup — fail fast on critical misconfigurations
    _validate_startup_configuration(settings)

    # Recover stale tasks: any task left IN_PROGRESS from a previous run
    # is marked FAILED (fail-closed restart recovery)
    recovered = task_tracker.recover_stale_tasks()
    if recovered:
        logger.warning(
            "Recovered stale in-progress tasks at startup",
            count=len(recovered),
            task_ids=[str(r.task_id) for r in recovered],
        )
    else:
        logger.info("No stale tasks to recover at startup")

    logger.info("BABY observability API ready")

    yield  # Application runs here

    # ---- SHUTDOWN ----
    logger.info("BABY observability API shutting down")

    # Mark all active workers as STOPPED on clean shutdown
    active_workers = worker_tracker.list_workers()
    for worker in active_workers:
        if worker.status not in (WorkerStatus.STOPPED, WorkerStatus.ERROR):
            worker_tracker.set_status(worker.worker_id, WorkerStatus.STOPPED)
            logger.info("Worker stopped on shutdown", worker_id=worker.worker_id)

    logger.info("BABY observability API shutdown complete")


def _validate_startup_configuration(settings_obj: object) -> None:
    """Validate critical settings at startup. Log warnings for misconfigurations.

    Critical issues (like invalid bounds) raise ConfigurationError.
    """
    from baby.configuration import Settings, validate_settings

    if isinstance(settings_obj, Settings):
        warnings = validate_settings(settings_obj)
        for w in warnings:
            logger.warning("Configuration warning", warning=w)
    logger.debug("Startup configuration validation complete")


app = FastAPI(
    title="BABY Observability API",
    description=(
        "Read-only operational observability and audit visibility API for the BABY platform. "
        "All endpoints enforce authentication and engagement scoping."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: object, exc: Exception):
    """Ensure unhandled server exceptions never leak stack traces to callers."""
    from fastapi.responses import JSONResponse

    logger.error("Unhandled server exception", error=str(exc))
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# Read-only enforcement middleware (applied first)
app.add_middleware(ReadOnlyMiddleware)

# Observability routers
app.include_router(health.router)
app.include_router(readiness.router)
app.include_router(metrics.router)
app.include_router(tasks.router)
app.include_router(workers.router)
app.include_router(agents.router)
app.include_router(audit.router)
