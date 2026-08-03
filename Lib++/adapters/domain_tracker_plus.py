"""
DomainTracker Plus — Enhanced DomainTracker with TLS profile per-domain learning.

Extends the proxy-orchestrator DomainTracker with:
  - TLS profile success tracking per domain
  - Best strategy + TLS profile combination learning
  - Adaptive strategy ordering based on TLS success
  - HTTP version success tracking per domain
  - Proxy + TLS profile correlation

Solves:
  - ❌ No TLS profile per-domain learning (original DomainTracker)
  - ❌ Strategy selection doesn't consider TLS fingerprint success
  - ❌ No HTTP version tracking per domain
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from ..core.tls_profiles import tls_profile_manager

logger = logging.getLogger(__name__)


@dataclass
class DomainTlsStats:
    """TLS profile + strategy success statistics for a domain."""
    tls_profile: str
    strategy: str
    success_count: int = 0
    failure_count: int = 0
    total_latency: float = 0.0
    last_used: float = 0.0
    http_versions: dict[str, int] = field(default_factory=dict)  # h2/h3/h1.1 -> success_count

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.0

    @property
    def avg_latency(self) -> float:
        return self.total_latency / self.success_count if self.success_count > 0 else 0.0


@dataclass
class DomainStats:
    """Aggregated domain statistics with TLS awareness."""
    total_requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    total_latency: float = 0.0
    best_tls_profile: str = ""
    best_strategy: str = ""
    tls_combinations: dict[str, DomainTlsStats] = field(default_factory=dict)
    last_request_time: float = 0.0


class DomainTrackerPlus:
    """
    Enhanced domain tracker with TLS profile learning.

    Tracks which (strategy + TLS profile + HTTP version) combinations
    work best for each domain. The decision engine uses this to select
    the optimal combination for each request.
    """

    def __init__(self):
        self._domains: dict[str, DomainStats] = {}
        self._lock = asyncio.Lock()

    async def record_success(
        self,
        domain: str,
        strategy: str,
        tls_profile: str,
        latency: float,
        http_version: str = "h2",
    ) -> None:
        """Record a successful request with its TLS profile."""
        async with self._lock:
            stats = self._get_or_create(domain)
            stats.total_requests += 1
            stats.successful_requests += 1
            stats.total_latency += latency
            stats.last_request_time = time.monotonic()

            key = f"{strategy}:{tls_profile}"
            if key not in stats.tls_combinations:
                stats.tls_combinations[key] = DomainTlsStats(
                    tls_profile=tls_profile,
                    strategy=strategy,
                )
            combo = stats.tls_combinations[key]
            combo.success_count += 1
            combo.total_latency += latency
            combo.last_used = time.monotonic()

            if http_version not in combo.http_versions:
                combo.http_versions[http_version] = 0
            combo.http_versions[http_version] += 1

            # Update best combination
            best_combo = max(
                stats.tls_combinations.values(),
                key=lambda c: c.success_rate,
                default=None,
            )
            if best_combo:
                stats.best_tls_profile = best_combo.tls_profile
                stats.best_strategy = best_combo.strategy

            # Also update global TLS profile manager
            tls_profile_manager.record_success(tls_profile, domain)

    async def record_failure(
        self,
        domain: str,
        strategy: str,
        tls_profile: str,
    ) -> None:
        """Record a failed request."""
        async with self._lock:
            stats = self._get_or_create(domain)
            stats.total_requests += 1
            stats.failed_requests += 1
            stats.last_request_time = time.monotonic()

            key = f"{strategy}:{tls_profile}"
            if key not in stats.tls_combinations:
                stats.tls_combinations[key] = DomainTlsStats(
                    tls_profile=tls_profile,
                    strategy=strategy,
                )
            stats.tls_combinations[key].failure_count += 1

            tls_profile_manager.record_failure(tls_profile, domain)

    async def get_best_combination(
        self,
        domain: str,
    ) -> tuple[str, str]:
        """
        Get the best (strategy, tls_profile) combination for a domain.
        Returns empty strings if no data available.
        """
        async with self._lock:
            stats = self._domains.get(domain)
            if not stats:
                return ("", "")

            best = max(
                stats.tls_combinations.values(),
                key=lambda c: c.success_rate,
                default=None,
            )
            if best:
                return (best.strategy, best.tls_profile)
            return ("", "")

    async def get_best_strategy(self, domain: str) -> str:
        """Get best strategy for a domain (backward compatible)."""
        strategy, _ = await self.get_best_combination(domain)
        return strategy

    async def get_best_tls_profile(self, domain: str) -> str:
        """Get best TLS profile for a domain."""
        _, profile = await self.get_best_combination(domain)
        return profile

    async def get_stats(self, domain: str) -> dict:
        """Get detailed stats for a domain."""
        async with self._lock:
            stats = self._domains.get(domain)
            if not stats:
                return {}

            best_combos = sorted(
                stats.tls_combinations.values(),
                key=lambda c: c.success_rate,
                reverse=True,
            )[:5]

            return {
                "total_requests": stats.total_requests,
                "success_rate": round(
                    stats.successful_requests / max(stats.total_requests, 1), 3
                ),
                "avg_latency": round(
                    stats.total_latency / max(stats.successful_requests, 1), 3
                ),
                "best_strategy": stats.best_strategy,
                "best_tls_profile": stats.best_tls_profile,
                "best_combinations": [
                    {
                        "strategy": c.strategy,
                        "tls_profile": c.tls_profile,
                        "success_rate": round(c.success_rate, 3),
                        "avg_latency": round(c.avg_latency, 3),
                        "http_versions": c.http_versions,
                    }
                    for c in best_combos
                ],
            }

    async def get_all_stats(self) -> dict[str, dict]:
        """Get stats for all tracked domains."""
        async with self._lock:
            return {
                domain: {
                    "total_requests": s.total_requests,
                    "success_rate": round(
                        s.successful_requests / max(s.total_requests, 1), 3
                    ),
                    "best_strategy": s.best_strategy,
                    "best_tls_profile": s.best_tls_profile,
                }
                for domain, s in self._domains.items()
            }

    def _get_or_create(self, domain: str) -> DomainStats:
        if domain not in self._domains:
            self._domains[domain] = DomainStats()
        return self._domains[domain]
