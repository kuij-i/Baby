"""Middleware ensuring read-only guarantees and metrics collection for observability API.

The API is read-only except for an explicit allow-list of task *control* endpoints
(submit, cancel, approval decision). Those only write durable task state; they never
execute tasks, agents or tools — execution happens exclusively in worker processes.
"""

import re
import time

from fastapi import Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from baby.observability.metrics import metrics_collector

READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
# The only (method, path) pairs allowed to change state. Everything else that is not a
# read returns 405. Each is mounted at the root and under /api/v1.
TASK_CONTROL_ENDPOINTS = (
    ("POST", re.compile(r"^(/api/v1)?/tasks/submit$")),
    ("POST", re.compile(rf"^(/api/v1)?/tasks/{_UUID}/cancel$")),
    ("POST", re.compile(rf"^(/api/v1)?/tasks/{_UUID}/approvals/{_UUID}$")),
)


def is_task_control_request(method: str, path: str) -> bool:
    return any(method == m and pattern.match(path) for m, pattern in TASK_CONTROL_ENDPOINTS)


class ReadOnlyMiddleware(BaseHTTPMiddleware):
    """Enforces that all requests to observability endpoints are strictly read-only."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in READ_ONLY_METHODS and not is_task_control_request(request.method, request.url.path):
            return Response(
                content='{"detail":"Method not allowed: Observability API is strictly read-only"}',
                status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
                media_type="application/json",
            )

        start_time = time.perf_counter()
        endpoint = request.url.path
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - start_time) * 1000
            metrics_collector.record_api_request(
                method=request.method,
                endpoint=endpoint,
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
            metrics_collector.record_latency("api_request_duration_ms", duration_ms, labels={"endpoint": endpoint})
            return Response(
                content='{"detail":"Internal server error"}',
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                media_type="application/json",
            )

        duration_ms = (time.perf_counter() - start_time) * 1000

        # Record API request metric
        metrics_collector.record_api_request(
            method=request.method,
            endpoint=endpoint,
            status_code=response.status_code,
        )
        metrics_collector.record_latency("api_request_duration_ms", duration_ms, labels={"endpoint": endpoint})

        return response
