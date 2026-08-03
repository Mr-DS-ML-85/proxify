"""
Request Deduplication — Concurrent identical requests share a single fetch result.
Uses asyncio futures to avoid redundant network calls.
"""

import asyncio
import logging
import time
from services.cache import L1Cache

from config import config

logger = logging.getLogger(__name__)


class RequestDedup:
    """
    Deduplicates concurrent identical requests.
    If two requests for the same URL arrive within the dedup window,
    the second one waits for the first to complete and shares its result.
    """

    def __init__(self, window: float = config.DEDUP_WINDOW) -> None:
        self._window = window
        self._pending: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()
        self._dedup_count = 0

    async def get_or_create(self, cache_key: str) -> tuple[asyncio.Future, bool]:
        """
        Get an existing pending future or create a new one.

        Returns:
            (future, is_new) — if is_new=True, caller must set the result on the future.
            If is_new=False, caller should await the future for the shared result.
        """
        async with self._lock:
            if cache_key in self._pending:
                future = self._pending[cache_key]
                if not future.done():
                    self._dedup_count += 1
                    logger.debug(f"Request dedup: sharing result for {cache_key[:16]}...")
                    return future, False

            # Create new future
            loop = asyncio.get_event_loop()
            future = loop.create_future()
            self._pending[cache_key] = future

            # Schedule cleanup after window expires
            asyncio.create_task(self._cleanup_after(cache_key))

            return future, True

    async def _cleanup_after(self, cache_key: str) -> None:
        """Remove the pending future after the dedup window."""
        await asyncio.sleep(self._window)
        async with self._lock:
            self._pending.pop(cache_key, None)

    @property
    def stats(self) -> dict:
        return {
            "pending_requests": len(self._pending),
            "dedup_saves": self._dedup_count,
        }
