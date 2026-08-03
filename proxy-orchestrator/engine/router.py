"""
Google-Safe Query Router — Deterministic routing with rate limiting.
Routes search queries through SearXNG/Whoogle first, only falls back to Google when required.
"""

import asyncio
import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RouteDecision:
    """Routing decision for a search query."""
    engine: str  # "searxng" | "whoogle" | "google_direct"
    delay: float  # seconds to wait before request
    use_cache: bool
    proxy_required: bool
    headers: dict[str, str]


class QueryRouter:
    """
    Routes search queries to minimize Google direct scraping.
    Deterministic rules, no AI.
    """

    def __init__(
        self,
        google_min_interval: float = 3.0,
        cache_ttl: int = 300,
    ) -> None:
        self._google_min_interval = google_min_interval
        self._cache_ttl = cache_ttl
        self._last_google_time: float = 0.0
        self._query_cache: dict[str, tuple[float, str]] = {}  # hash -> (timestamp, engine)
        self._lock = asyncio.Lock()

    def _hash_query(self, query: str) -> str:
        return hashlib.md5(query.lower().strip().encode()).hexdigest()

    async def route_query(
        self,
        query: str,
        force_google: bool = False,
        searxng_available: bool = True,
        whoogle_available: bool = False,
    ) -> RouteDecision:
        """
        Route a search query to the best engine.

        Rules:
        - Default: SearXNG or Whoogle
        - Google direct only if: fallback required AND low request frequency
        - Add delay (1-3s) for Google
        - Cache repeated queries
        """
        query_hash = self._hash_query(query)

        async with self._lock:
            # Check query cache
            if query_hash in self._query_cache:
                ts, cached_engine = self._query_cache[query_hash]
                if time.monotonic() - ts < self._cache_ttl:
                    return RouteDecision(
                        engine=cached_engine,
                        delay=0.0,
                        use_cache=True,
                        proxy_required=False,
                        headers={},
                    )

            # Route decision
            if not force_google:
                if searxng_available:
                    engine = "searxng"
                elif whoogle_available:
                    engine = "whoogle"
                else:
                    engine = "google_direct"
            else:
                engine = "google_direct"

            # Google-specific safety
            delay = 0.0
            proxy_required = False
            headers: dict[str, str] = {}

            if engine == "google_direct":
                # Enforce minimum interval
                elapsed = time.monotonic() - self._last_google_time
                if elapsed < self._google_min_interval:
                    delay = self._google_min_interval - elapsed + random.uniform(0.5, 1.5)
                else:
                    delay = random.uniform(1.0, 3.0)

                proxy_required = True
                self._last_google_time = time.monotonic() + delay

                # Random Accept-Language for diversity
                langs = [
                    "en-US,en;q=0.9",
                    "en-GB,en;q=0.9",
                    "en-US,en;q=0.9,de;q=0.8",
                    "en-US,en;q=0.9,fr;q=0.8",
                ]
                headers["Accept-Language"] = random.choice(langs)

            # Cache the decision
            self._query_cache[query_hash] = (time.monotonic(), engine)

            logger.debug(
                f"router: query='{query[:30]}...' engine={engine} "
                f"delay={delay:.1f}s proxy={proxy_required}"
            )

            return RouteDecision(
                engine=engine,
                delay=delay,
                use_cache=False,
                proxy_required=proxy_required,
                headers=headers,
            )

    async def cleanup(self) -> None:
        """Remove expired query cache entries."""
        async with self._lock:
            now = time.monotonic()
            expired = [
                k for k, (ts, _) in self._query_cache.items()
                if now - ts > self._cache_ttl
            ]
            for k in expired:
                del self._query_cache[k]


# Global singleton
query_router = QueryRouter()
