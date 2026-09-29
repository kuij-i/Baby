"""Bounded operational metrics collection layer for BABY.

Provides a thread-safe, bounded, testable metrics collector tracking task
executions, latencies, worker activity, approvals, verification results,
tool calls, error rates, and API usage.
"""

import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class LatencyStats:
    """Latency summary statistics."""

    count: int = 0
    total_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0

    @property
    def avg_ms(self) -> float:
        return (self.total_ms / self.count) if self.count > 0 else 0.0

    def record(self, duration_ms: float) -> None:
        if self.count == 0:
            self.min_ms = duration_ms
            self.max_ms = duration_ms
        else:
            if duration_ms < self.min_ms:
                self.min_ms = duration_ms
            if duration_ms > self.max_ms:
                self.max_ms = duration_ms
        self.count += 1
        self.total_ms += duration_ms

    def to_dict(self) -> Dict[str, Any]:
        return {
            "count": self.count,
            "total_ms": round(self.total_ms, 2),
            "avg_ms": round(self.avg_ms, 2),
            "min_ms": round(self.min_ms, 2) if self.count > 0 else 0.0,
            "max_ms": round(self.max_ms, 2) if self.count > 0 else 0.0,
        }


class MetricsCollector:
    """Bounded, thread-safe metrics collector."""

    MAX_SERIES_PER_METRIC = 200

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, Dict[str, int]] = {}
        self._gauges: Dict[str, Dict[str, float]] = {}
        self._latencies: Dict[str, Dict[str, LatencyStats]] = {}

    def _labels_to_key(self, labels: Optional[Dict[str, str]]) -> str:
        if not labels:
            return ""
        return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))

    def increment_counter(self, name: str, labels: Optional[Dict[str, str]] = None, value: int = 1) -> None:
        """Increment a counter metric."""
        with self._lock:
            if name not in self._counters:
                self._counters[name] = {}
            series = self._counters[name]
            key = self._labels_to_key(labels)
            if key not in series:
                if len(series) >= self.MAX_SERIES_PER_METRIC:
                    key = "_overflow"
                series[key] = 0
            series[key] += value

    def set_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Set a gauge metric."""
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = {}
            series = self._gauges[name]
            key = self._labels_to_key(labels)
            if key not in series and len(series) >= self.MAX_SERIES_PER_METRIC:
                key = "_overflow"
            series[key] = value

    def record_latency(self, name: str, duration_ms: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Record an operation latency measurement."""
        with self._lock:
            if name not in self._latencies:
                self._latencies[name] = {}
            series = self._latencies[name]
            key = self._labels_to_key(labels)
            if key not in series:
                if len(series) >= self.MAX_SERIES_PER_METRIC:
                    key = "_overflow"
                series[key] = LatencyStats()
            series[key].record(duration_ms)

    # Convenience domain helpers
    def record_task_created(self, priority: int = 0) -> None:
        self.increment_counter("tasks_created_total", labels={"priority": str(priority)})
        self.increment_counter("tasks_by_status", labels={"status": "pending"})

    def record_task_started(self) -> None:
        self.increment_counter("tasks_by_status", labels={"status": "in_progress"})

    def record_task_completed(self, duration_ms: float) -> None:
        self.increment_counter("tasks_completed_total")
        self.increment_counter("tasks_by_status", labels={"status": "completed"})
        self.record_latency("task_execution_duration_ms", duration_ms)

    def record_task_failed(self, duration_ms: float, error_type: str = "generic") -> None:
        self.increment_counter("tasks_failed_total", labels={"error_type": error_type})
        self.increment_counter("tasks_by_status", labels={"status": "failed"})
        self.record_latency("task_execution_duration_ms", duration_ms)
        self.increment_counter("errors_total", labels={"type": error_type})

    def record_approval_requested(self) -> None:
        self.increment_counter("approvals_total", labels={"status": "requested"})

    def record_approval_decision(self, approved: bool) -> None:
        status = "granted" if approved else "denied"
        self.increment_counter("approvals_total", labels={"status": status})

    def record_verification(self, verified: bool) -> None:
        status = "passed" if verified else "failed"
        self.increment_counter("verifications_total", labels={"status": status})

    def record_tool_execution(self, tool_name: str, success: bool, duration_ms: float = 0.0) -> None:
        status = "success" if success else "failure"
        self.increment_counter("tool_executions_total", labels={"tool": tool_name, "status": status})
        if duration_ms > 0:
            self.record_latency("tool_execution_duration_ms", duration_ms, labels={"tool": tool_name})

    def record_api_request(self, method: str, endpoint: str, status_code: int) -> None:
        # Normalize endpoint to a bounded prefix to prevent label cardinality abuse.
        # Strip /api/v1 prefix if present so versioned endpoints share bounded label space.
        clean_endpoint = endpoint
        if clean_endpoint.startswith("/api/v1"):
            clean_endpoint = clean_endpoint[7:] or "/"

        known_prefixes = {"/health", "/ready", "/readiness", "/metrics", "/tasks", "/workers", "/agents", "/audit"}
        endpoint_label = "other"
        for prefix in known_prefixes:
            if clean_endpoint == prefix or clean_endpoint.startswith(prefix + "/"):
                endpoint_label = prefix
                break
        self.increment_counter(
            "api_requests_total",
            labels={"method": method, "endpoint": endpoint_label, "status": str(status_code)},
        )

    def record_error(self, error_type: str) -> None:
        self.increment_counter("errors_total", labels={"type": error_type})

    def get_snapshot(self) -> Dict[str, Any]:
        """Produce a complete read-only snapshot of all metric series."""
        with self._lock:
            counters_snap: Dict[str, Dict[str, int]] = {name: dict(series) for name, series in self._counters.items()}
            gauges_snap: Dict[str, Dict[str, float]] = {name: dict(series) for name, series in self._gauges.items()}
            latencies_snap: Dict[str, Dict[str, Dict[str, Any]]] = {
                name: {k: v.to_dict() for k, v in series.items()} for name, series in self._latencies.items()
            }
            return {
                "counters": counters_snap,
                "gauges": gauges_snap,
                "latencies": latencies_snap,
            }

    def reset(self) -> None:
        """Reset all metrics; intended for testing."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._latencies.clear()


# Global metrics collector instance
metrics_collector = MetricsCollector()
