"""
Domain Tracker — Per-domain statistics for adaptive strategy selection.
Tracks success rates, latency, and best-performing strategy per domain.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class DomainStats:
    """Statistics for a specific domain."""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_latency: float = 0.0
    strategy_successes: dict[str, int] = field(default_factory=dict)
    strategy_failures: dict[str, int] = field(default_factory=dict)
    last_success_strategy: str = ""
    last_request_time: float = 0.0
    last_success_time: float = 0.0

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.successful_requests / self.total_requests

    @property
    def avg_latency(self) -> float:
        if self.successful_requests == 0:
            return 0.0
        return self.total_latency / self.successful_requests

    @property
    def best_strategy(self) -> str:
        """Return the strategy with the highest success count for this domain."""
        if not self.strategy_successes:
            return ""
        return max(self.strategy_successes, key=self.strategy_successes.get)


class DomainTracker:
    """
    Tracks per-domain performance metrics for adaptive strategy selection.
    The Decision Engine uses this data to select the optimal strategy for each domain.
    """

    def __init__(self) -> None:
        self._domains: dict[str, DomainStats] = {}
        self._lock = asyncio.Lock()

    async def record_success(
        self,
        domain: str,
        strategy: str,
        latency: float,
    ) -> None:
        """Record a successful fetch for a domain."""
        async with self._lock:
            stats = self._get_or_create(domain)
            stats.total_requests += 1
            stats.successful_requests += 1
            stats.total_latency += latency
            stats.last_success_strategy = strategy
            stats.last_success_time = time.monotonic()
            stats.last_request_time = time.monotonic()
            stats.strategy_successes[strategy] = (
                stats.strategy_successes.get(strategy, 0) + 1
            )

    async def record_failure(
        self,
        domain: str,
        strategy: str,
    ) -> None:
        """Record a failed fetch for a domain."""
        async with self._lock:
            stats = self._get_or_create(domain)
            stats.total_requests += 1
            stats.failed_requests += 1
            stats.last_request_time = time.monotonic()
            stats.strategy_failures[strategy] = (
                stats.strategy_failures.get(strategy, 0) + 1
            )

    async def get_best_strategy(self, domain: str) -> str:
        """Get the best-performing strategy for a domain."""
        async with self._lock:
            stats = self._domains.get(domain)
            if stats is None or not stats.strategy_successes:
                return ""
            return stats.best_strategy

    async def get_stats(self, domain: str) -> dict:
        """Get stats for a specific domain."""
        async with self._lock:
            stats = self._domains.get(domain)
            if stats is None:
                return {}
            return {
                "total_requests": stats.total_requests,
                "success_rate": round(stats.success_rate, 3),
                "avg_latency": round(stats.avg_latency, 3),
                "best_strategy": stats.best_strategy,
                "last_success_strategy": stats.last_success_strategy,
                "strategy_successes": dict(stats.strategy_successes),
                "strategy_failures": dict(stats.strategy_failures),
            }

    async def get_all_stats(self) -> dict[str, dict]:
        """Get stats for all tracked domains."""
        async with self._lock:
            return {
                domain: {
                    "total_requests": s.total_requests,
                    "success_rate": round(s.success_rate, 3),
                    "avg_latency": round(s.avg_latency, 3),
                    "best_strategy": s.best_strategy,
                }
                for domain, s in self._domains.items()
            }

    def _get_or_create(self, domain: str) -> DomainStats:
        if domain not in self._domains:
            self._domains[domain] = DomainStats()
        return self._domains[domain]
