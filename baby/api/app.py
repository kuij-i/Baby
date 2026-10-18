"""BABY FastAPI observability application.

Provides read-only operational endpoints for health, metrics, tasks,
workers, agents, and audit visibility. All endpoints are protected by
the existing authentication and engagement-scoping model.

Phase 10 & 11 additions:
- FastAPI lifespan handler for deterministic startup/shutdown
- Startup: validates configuration, initializes durable database, recovers stale tasks
- Shutdown: stops workers cleanly, closes database resources
- Clean API versioning (/api/v1) alongside backwards-compatible unversioned endpoints
  for future TypeScript clients

Phase 12 additions:
- Durable audit backend attached at startup (audit_storage_type='sqlite') so events from
  isolated worker processes are visible; detached again at shutdown
- Lease-aware recovery: tasks owned by live workers (valid lease) survive an API restart;
  expired/orphaned work fails closed
- Optional worker supervision (worker_enabled=True): worker *processes* are started and
  stopped with the API. The API itself never executes tasks.
"""

from contextlib import asynccontextmanager
from functools import lru_cache
from importlib import resources
from typing import AsyncIterator

from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse

from baby.api.middleware import ReadOnlyMiddleware
from baby.api.routes import agents, audit, health, metrics, readiness, tasks, workers
from baby.logging import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: deterministic startup and shutdown.

    Startup:
        1. Validate critical configuration
        2. Initialize durable database schema
        3. Recover stale in-progress tasks (fail-closed restart recovery)
        4. Log readiness

    Shutdown:
        1. Log shutdown signal
        2. Transition active workers to STOPPED
        3. Close persistent database connections
        4. Log clean shutdown
    """
    # ---- STARTUP ----
    # Import here to avoid circular imports at module load time
    from baby.configuration import settings
    from baby.core import WorkerStatus
    from baby.logging import configure_logging
    from baby.observability.tasks import task_tracker
    from baby.observability.workers import worker_tracker
    from baby.persistence.database import db_manager

    configure_logging(settings.log_level)
    logger.info("BABY observability API starting up")

    # 1. Validate configuration at startup — fail fast on critical misconfigurations
    _validate_startup_configuration(settings)

    # Providers are registered only to report their availability on /health; the API never
    # calls a model (workers register their own). Without credentials nothing is registered.
    from baby.providers.registry import register_configured_providers

    register_configured_providers()

    # 2. Initialize database schema if SQLite persistence is enabled
    if getattr(settings, "persistence_backend", "sqlite") == "sqlite":
        try:
            db_manager.init_schema()
        except Exception as exc:
            logger.error("Failed to initialize database schema", error=str(exc))
            raise

    previous_audit_backend = None
    audit_backend_attached = False
    if settings.persistence_backend == "sqlite" and settings.audit_storage_type == "sqlite":
        from baby.audit import audit_log
        from baby.persistence.sqlite_audit import SQLiteAuditLog

        previous_audit_backend = audit_log.use_backend(SQLiteAuditLog(db_manager))
        audit_backend_attached = True

    # 3. Recover stale work (fail closed, never replayed):
    #    a) leases whose worker is stale/gone or whose lease expired → task FAILED
    #    b) IN_PROGRESS tasks with no lease (no live owner)          → task FAILED
    #    Tasks leased by live workers are left alone (API restart while workers run).
    if task_tracker.store is not None:
        from baby.workers.recovery import WorkerRecovery

        lease_report = WorkerRecovery(task_store=task_tracker.store).recover()
        if lease_report.changed:
            logger.warning(
                "Recovered lost worker leases at startup",
                stale_workers=len(lease_report.stale_workers),
                failed_tasks=len(lease_report.failed_tasks),
            )
    recovered = task_tracker.recover_stale_tasks()
    if recovered:
        logger.warning(
            "Recovered stale in-progress tasks at startup",
            count=len(recovered),
            task_ids=[str(r.task_id) for r in recovered],
        )
    else:
        logger.info("No stale tasks to recover at startup")

    supervisor = None
    if settings.worker_enabled:
        from baby.workers.supervisor import WorkerSupervisor

        supervisor = WorkerSupervisor()
        supervisor.start()
        supervisor.start_monitor_thread()
        application.state.worker_supervisor = supervisor

    logger.info("BABY observability API ready")

    yield  # Application runs here

    # ---- SHUTDOWN ----
    logger.info("BABY observability API shutting down")

    if supervisor is not None:
        supervisor.stop()

    # Mark in-process tracked workers as STOPPED on clean shutdown. Durable worker
    # processes are independent of the API and keep running (and keep their leases).
    active_workers = worker_tracker.list_workers()
    for worker in active_workers:
        if worker.status not in (WorkerStatus.STOPPED, WorkerStatus.ERROR):
            worker_tracker.set_status(worker.worker_id, WorkerStatus.STOPPED)
            logger.info("Worker stopped on shutdown", worker_id=worker.worker_id)

    if audit_backend_attached:
        from baby.audit import audit_log

        audit_log.use_backend(previous_audit_backend)

    # Close database connections cleanly
    try:
        db_manager.close()
    except Exception as exc:
        logger.warning("Error closing database connection during shutdown", error=str(exc))

    logger.info("BABY observability API shutdown complete")


def _validate_startup_configuration(settings_obj: object) -> None:
    """Validate critical settings at startup. Log warnings for misconfigurations.

    Critical issues (like invalid bounds or missing database configuration) raise ConfigurationError.
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
        "Operational observability and audit visibility API for the BABY platform. Read-only except for "
        "task control (submit, cancel, admin approval decisions), which only writes durable task state; "
        "tasks execute in isolated worker processes. All endpoints enforce authentication and engagement scoping."
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

# Observability routers (unversioned for backwards compatibility)
app.include_router(health.router)
app.include_router(readiness.router)
app.include_router(metrics.router)
app.include_router(tasks.router)
app.include_router(workers.router)
app.include_router(agents.router)
app.include_router(audit.router)

# Versioned API routes (/api/v1) for future TypeScript clients
v1_router = APIRouter(prefix="/api/v1")
v1_router.include_router(health.router)
v1_router.include_router(readiness.router)
v1_router.include_router(metrics.router)
v1_router.include_router(tasks.router)
v1_router.include_router(workers.router)
v1_router.include_router(agents.router)
v1_router.include_router(audit.router)
app.include_router(v1_router)


@app.get("/", include_in_schema=False)
def root() -> dict:
    """Static landing response so the server root is not a bare 404. Exposes no state."""
    return {
        "name": "BABY Observability API",
        "version": app.version,
        "api": "/api/v1",
        "health": "/api/v1/health",
        "docs": "/docs",
        "chat": "/chat",
    }


# The chat page is static: it holds no data and calls the authenticated API from the browser
# with the token the user pastes in. Model output is escaped client-side before rendering.
CHAT_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


@lru_cache(maxsize=1)
def _chat_page_html() -> str:
    return resources.files("baby.api").joinpath("static/chat.html").read_text(encoding="utf-8")


@app.get("/chat", include_in_schema=False, response_class=HTMLResponse)
def chat_page() -> HTMLResponse:
    """Browser chat UI: submit a message as a task, watch the worker's answer, decide approvals."""
    return HTMLResponse(_chat_page_html(), headers=CHAT_PAGE_HEADERS)
