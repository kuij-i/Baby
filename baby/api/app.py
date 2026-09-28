"""BABY FastAPI observability application.

Provides read-only operational endpoints for health, metrics, tasks,
workers, agents, and audit visibility. All endpoints are protected by
the existing authentication and engagement-scoping model.
"""

from fastapi import FastAPI

from baby.api.middleware import ReadOnlyMiddleware
from baby.api.routes import agents, audit, health, metrics, tasks, workers

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
)

# Read-only enforcement middleware (applied first)
app.add_middleware(ReadOnlyMiddleware)

# Observability routers
app.include_router(health.router)
app.include_router(metrics.router)
app.include_router(tasks.router)
app.include_router(workers.router)
app.include_router(agents.router)
app.include_router(audit.router)
