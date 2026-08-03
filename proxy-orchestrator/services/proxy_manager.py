"""
Proxy Manager — Upstream proxy pool with rotation strategies and health checking.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Optional

from config import config

logger = logging.getLogger(__name__)


@dataclass
class ProxyInfo:
    """Tracks health and assignment of an upstream proxy."""
    url: str
    healthy: bool = True
    last_check: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    latency_sum: float = 0.0


class ProxyManager:
    """
    Upstream proxy pool management with rotation strategies.
    - round_robin: sequential rotation
    - random: random selection
    - sticky: same proxy for same domain
    Health checks periodically test proxies and remove dead ones.
    """

    def __init__(self) -> None:
        self._proxies: list[ProxyInfo] = []
        self._robin_index = 0
        self._sticky_map: dict[str, str] = {}  # domain -> proxy_url
        self._strategy = config.PROXY_ROTATION_STRATEGY
        self._lock = asyncio.Lock()

        # Initialize proxy pool from config
        for url in config.UPSTREAM_PROXIES:
            self._proxies.append(ProxyInfo(url=url))

        if self._proxies:
            logger.info(f"Proxy pool initialized with {len(self._proxies)} proxies")

    @property
    def has_proxies(self) -> bool:
        return len(self._proxies) > 0

    async def get_proxy(self, domain: Optional[str] = None, force_new: bool = False) -> Optional[str]:
        """
        Get the next proxy URL based on the configured rotation strategy.
        Returns None if no proxies are available.
        """
        async with self._lock:
            healthy = [p for p in self._proxies if p.healthy]
            if not healthy:
                return None

            if self._strategy == "sticky" and domain and not force_new:
                # Sticky: same proxy for same domain
                if domain in self._sticky_map:
                    url = self._sticky_map[domain]
                    # Check if the sticky proxy is still healthy
                    if any(p.url == url and p.healthy for p in self._proxies):
                        return url
                # Assign new sticky proxy
                proxy = random.choice(healthy)
                self._sticky_map[domain] = proxy.url
                return proxy.url

            elif self._strategy == "random":
                return random.choice(healthy).url

            else:  # round_robin (default)
                proxy = healthy[self._robin_index % len(healthy)]
                self._robin_index = (self._robin_index + 1) % len(healthy)
                return proxy.url

    async def record_success(self, proxy_url: str, latency: float) -> None:
        """Record a successful request through a proxy."""
        async with self._lock:
            for p in self._proxies:
                if p.url == proxy_url:
                    p.success_count += 1
                    p.latency_sum += latency
                    p.healthy = True
                    # Reset so failure_count tracks *consecutive* failures.
                    p.failure_count = 0
                    break

    async def record_failure(self, proxy_url: str) -> None:
        """Record a failed request through a proxy."""
        async with self._lock:
            for p in self._proxies:
                if p.url == proxy_url:
                    p.failure_count += 1
                    # Mark unhealthy after 3 consecutive failures
                    if p.failure_count > 3:
                        p.healthy = False
                        logger.warning(f"Proxy marked unhealthy: {proxy_url}")
                    break

    async def health_check(self) -> dict[str, bool]:
        """Run health checks on all proxies. Returns url -> healthy map."""
        import httpx

        results = {}
        for proxy_info in self._proxies:
            try:
                async with httpx.AsyncClient(
                    proxy=proxy_info.url,
                    timeout=10.0,
                ) as client:
                    resp = await client.get("https://httpbin.org/ip")
                    healthy = resp.status_code == 200
            except Exception:
                healthy = False

            async with self._lock:
                proxy_info.healthy = healthy
                proxy_info.last_check = time.monotonic()

            results[proxy_info.url] = healthy
            logger.debug(f"Proxy health check {proxy_info.url}: {'OK' if healthy else 'FAIL'}")

        return results

    async def add_proxy(self, url: str) -> None:
        """Add a new proxy to the pool."""
        async with self._lock:
            if not any(p.url == url for p in self._proxies):
                self._proxies.append(ProxyInfo(url=url))
                logger.info(f"Proxy added: {url}")

    async def remove_proxy(self, url: str) -> None:
        """Remove a proxy from the pool."""
        async with self._lock:
            self._proxies = [p for p in self._proxies if p.url != url]
            # Clean sticky map
            self._sticky_map = {
                d: u for d, u in self._sticky_map.items() if u != url
            }
            logger.info(f"Proxy removed: {url}")

    async def get_stats(self) -> list[dict]:
        """Get proxy pool statistics."""
        async with self._lock:
            return [
                {
                    "url": p.url,
                    "healthy": p.healthy,
                    "successes": p.success_count,
                    "failures": p.failure_count,
                    "avg_latency": (
                        round(p.latency_sum / p.success_count, 3)
                        if p.success_count > 0
                        else None
                    ),
                }
                for p in self._proxies
            ]
