"""
Prometheus Metrics — Real-time observability for the proxy orchestrator.
Exposes /metrics endpoint with Prometheus-format counters and histograms.
"""

import logging
import time
from typing import Optional

from prometheus_client import (
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

logger = logging.getLogger(__name__)

# === Counters ===
REQUESTS_TOTAL = Counter(
    "proxy_orchestrator_requests_total",
    "Total number of requests processed",
    ["method", "domain", "strategy", "status"],
)

CACHE_OPERATIONS = Counter(
    "proxy_orchestrator_cache_operations_total",
    "Cache operations",
    ["layer", "operation"],  # layer=l1/l2, operation=hit/miss/set
)

STRATEGY_USAGE = Counter(
    "proxy_orchestrator_strategy_usage_total",
    "Strategy usage count",
    ["strategy", "result"],  # result=success/failure/fallback
)

CIRCUIT_BREAKER_EVENTS = Counter(
    "proxy_orchestrator_circuit_breaker_events_total",
    "Circuit breaker state changes",
    ["strategy", "domain", "event"],  # event=opened/closed/half_open
)

# === Histograms ===
REQUEST_LATENCY = Histogram(
    "proxy_orchestrator_request_duration_seconds",
    "Request duration in seconds",
    ["strategy", "domain"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0],
)

# === Gauges ===
ACTIVE_SESSIONS = Gauge(
    "proxy_orchestrator_active_sessions",
    "Number of active sessions",
)

CACHE_SIZE = Gauge(
    "proxy_orchestrator_cache_size",
    "Current cache size",
    ["layer"],
)

HEALTHY_PROXIES = Gauge(
    "proxy_orchestrator_healthy_proxies",
    "Number of healthy upstream proxies",
)

# === Info ===
BUILD_INFO = Info(
    "proxy_orchestrator",
    "Proxify build information",
)
BUILD_INFO.info({
    "version": "1.0.0",
    "name": "proxy-orchestrator",
})


class MetricsCollector:
    """Convenience wrapper for recording metrics."""

    @staticmethod
    def record_request(
        method: str,
        domain: str,
        strategy: str,
        status_code: int,
        latency: float,
    ) -> None:
        """Record a completed request."""
        status_bucket = "2xx" if 200 <= status_code < 300 else (
            "4xx" if 400 <= status_code < 500 else (
                "5xx" if 500 <= status_code < 600 else "other"
            )
        )
        REQUESTS_TOTAL.labels(
            method=method,
            domain=domain,
            strategy=strategy,
            status=status_bucket,
        ).inc()
        REQUEST_LATENCY.labels(
            strategy=strategy,
            domain=domain,
        ).observe(latency)

    @staticmethod
    def record_cache_hit(layer: str) -> None:
        CACHE_OPERATIONS.labels(layer=layer, operation="hit").inc()

    @staticmethod
    def record_cache_miss(layer: str) -> None:
        CACHE_OPERATIONS.labels(layer=layer, operation="miss").inc()

    @staticmethod
    def record_cache_set(layer: str) -> None:
        CACHE_OPERATIONS.labels(layer=layer, operation="set").inc()

    @staticmethod
    def record_strategy(strategy: str, result: str) -> None:
        STRATEGY_USAGE.labels(strategy=strategy, result=result).inc()

    @staticmethod
    def record_circuit_event(strategy: str, domain: str, event: str) -> None:
        CIRCUIT_BREAKER_EVENTS.labels(
            strategy=strategy, domain=domain, event=event
        ).inc()

    @staticmethod
    def set_active_sessions(count: int) -> None:
        ACTIVE_SESSIONS.set(count)

    @staticmethod
    def set_cache_size(layer: str, size: int) -> None:
        CACHE_SIZE.labels(layer=layer).set(size)

    @staticmethod
    def set_healthy_proxies(count: int) -> None:
        HEALTHY_PROXIES.set(count)

    @staticmethod
    def generate() -> bytes:
        """Generate Prometheus-format metrics output."""
        return generate_latest()

    @staticmethod
    def content_type() -> str:
        return CONTENT_TYPE_LATEST


metrics = MetricsCollector()
