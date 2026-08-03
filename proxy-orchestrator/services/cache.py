"""
In-memory L1 Cache with TTL eviction & LRU ordering.
Provides MeiliSearch-like ultra-fast sub-millisecond reads for repeat queries.
"""

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from config import config

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """A cached response with expiration."""
    value: dict
    expires_at: float
    created_at: float


class L1Cache:
    """
    Ultra-fast in-memory LRU cache with TTL.
    Thread-safe via asyncio lock.
    """

    def __init__(
        self,
        max_size: int = config.L1_CACHE_MAX_SIZE,
        default_ttl: int = config.L1_CACHE_TTL,
    ) -> None:
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._store: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def make_key(
        method: str, 
        url: str, 
        params: Optional[dict] = None, 
        session_id: Optional[str] = None
    ) -> str:
        """Generate a deterministic cache key from request parameters."""
        parts = [method.upper(), url]
        if params:
            parts.append(json.dumps(params, sort_keys=True))
        if session_id:
            parts.append(session_id)
            
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()

    async def get(self, key: str) -> Optional[dict]:
        """Get a cached value. Returns None if not found or expired."""
        async with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None

            if time.monotonic() > entry.expires_at:
                # Expired — remove
                del self._store[key]
                self._misses += 1
                return None

            # Move to end (most recently used)
            self._store.move_to_end(key)
            self._hits += 1
            return entry.value

    async def set(self, key: str, value: dict, ttl: Optional[int] = None) -> None:
        """Store a value with TTL."""
        effective_ttl = ttl if ttl is not None else self._default_ttl
        async with self._lock:
            now = time.monotonic()
            self._store[key] = CacheEntry(
                value=value,
                expires_at=now + effective_ttl,
                created_at=now,
            )
            self._store.move_to_end(key)

            # Evict oldest if over capacity
            while len(self._store) > self._max_size:
                evicted_key, _ = self._store.popitem(last=False)
                logger.debug(f"L1 cache evicted: {evicted_key[:16]}...")

    async def delete(self, key: str) -> bool:
        """Delete a specific key. Returns True if it existed."""
        async with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    async def clear(self) -> int:
        """Clear all entries. Returns count of cleared entries."""
        async with self._lock:
            count = len(self._store)
            self._store.clear()
            return count

    async def cleanup_expired(self) -> int:
        """Remove all expired entries. Returns count of removed entries."""
        async with self._lock:
            now = time.monotonic()
            expired_keys = [
                k for k, v in self._store.items() if now > v.expires_at
            ]
            for k in expired_keys:
                del self._store[k]
            return len(expired_keys)

    @property
    def stats(self) -> dict:
        return {
            "size": len(self._store),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self._hits / max(self._hits + self._misses, 1),
        }
