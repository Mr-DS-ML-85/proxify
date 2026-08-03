"""
Redis L2 Cache — Persistent shared cache across sessions.
Falls back gracefully if Redis is unavailable.
"""

import json
import logging
from typing import Optional

from config import config

logger = logging.getLogger(__name__)

# Lazy import to avoid hard dependency
_redis_client = None


async def _get_redis():
    """Lazy initialization of async Redis client."""
    global _redis_client
    if _redis_client is None and config.REDIS_ENABLED:
        try:
            import redis.asyncio as aioredis
            _redis_client = aioredis.from_url(
                config.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
            # Test connection
            await _redis_client.ping()
            logger.info(f"Redis connected: {config.REDIS_URL}")
        except Exception as e:
            logger.warning(f"Redis connection failed (will operate without L2 cache): {e}")
            _redis_client = None
    return _redis_client


class RedisCache:
    """
    L2 Redis cache for persistent, shared caching.
    Gracefully degrades if Redis is unavailable.
    """

    def __init__(self, default_ttl: int = config.L2_CACHE_TTL) -> None:
        self._default_ttl = default_ttl
        self._available = config.REDIS_ENABLED

    async def get(self, key: str) -> Optional[dict]:
        """Get a cached value from Redis."""
        if not self._available:
            return None
        try:
            client = await _get_redis()
            if client is None:
                return None
            raw = await client.get(f"po:cache:{key}")
            if raw:
                return json.loads(raw)
        except Exception as e:
            logger.debug(f"Redis GET error: {e}")
        return None

    async def set(self, key: str, value: dict, ttl: Optional[int] = None) -> None:
        """Store a value in Redis with TTL."""
        if not self._available:
            return
        try:
            client = await _get_redis()
            if client is None:
                return
            effective_ttl = ttl if ttl is not None else self._default_ttl
            serialized = json.dumps(value, default=str)
            await client.setex(f"po:cache:{key}", effective_ttl, serialized)
        except Exception as e:
            logger.debug(f"Redis SET error: {e}")

    async def delete(self, key: str) -> bool:
        """Delete a cached value."""
        if not self._available:
            return False
        try:
            client = await _get_redis()
            if client is None:
                return False
            result = await client.delete(f"po:cache:{key}")
            return result > 0
        except Exception as e:
            logger.debug(f"Redis DELETE error: {e}")
            return False

    async def clear_pattern(self, pattern: str = "*") -> int:
        """Clear all keys matching pattern."""
        if not self._available:
            return 0
        try:
            client = await _get_redis()
            if client is None:
                return 0
            keys = []
            async for key in client.scan_iter(f"po:cache:{pattern}"):
                keys.append(key)
            if keys:
                return await client.delete(*keys)
        except Exception as e:
            logger.debug(f"Redis CLEAR error: {e}")
        return 0

    async def close(self) -> None:
        """Close the Redis connection."""
        global _redis_client
        if _redis_client:
            await _redis_client.close()
            _redis_client = None
