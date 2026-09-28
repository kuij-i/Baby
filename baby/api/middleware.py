"""Middleware ensuring read-only guarantees and metrics collection for observability API."""

import time

from fastapi import Request, Response, status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from baby.observability.metrics import metrics_collector

READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}


class ReadOnlyMiddleware(BaseHTTPMiddleware):
    """Enforces that all requests to observability endpoints are strictly read-only."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in READ_ONLY_METHODS:
            return Response(
                content='{"detail":"Method not allowed: Observability API is strictly read-only"}',
                status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
                media_type="application/json",
            )

        start_time = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start_time) * 1000

        # Record API request metric
        endpoint = request.url.path
        metrics_collector.record_api_request(
            method=request.method,
            endpoint=endpoint,
            status_code=response.status_code,
        )
        metrics_collector.record_latency("api_request_duration_ms", duration_ms, labels={"endpoint": endpoint})

        return response
