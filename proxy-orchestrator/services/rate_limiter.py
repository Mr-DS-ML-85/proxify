"""
Rate Limiter — Token bucket per domain + global rate limiting.
Auto-throttles on 429/503 responses.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional

from config import config

logger = logging.getLogger(__name__)


@dataclass
class TokenBucket:
    """A token bucket for rate limiting."""
    capacity: float
    tokens: float
    refill_rate: float  # tokens per second
    last_refill: float


class RateLimiter:
    """
    Dual-layer token bucket rate limiter.
    - Global bucket limits total request rate
    - Per-domain buckets limit requests to individual domains
    - Auto-throttle: reduces limits on 429/503 responses
    """

    def __init__(
        self,
        global_rate: int = config.GLOBAL_RATE_LIMIT,
        per_domain_rate: int = config.PER_DOMAIN_RATE_LIMIT,
    ) -> None:
        self._global_rate = global_rate
        self._per_domain_rate = per_domain_rate
        self._global_bucket = TokenBucket(
            capacity=float(global_rate),
            tokens=float(global_rate),
            refill_rate=float(global_rate),
            last_refill=time.monotonic(),
        )
        self._domain_buckets: dict[str, TokenBucket] = {}
        self._throttled_domains: dict[str, float] = {}  # domain -> throttle_until
        self._lock = asyncio.Lock()

    def _get_domain_bucket(self, domain: str) -> TokenBucket:
        """Get or create a token bucket for a domain."""
        if domain not in self._domain_buckets:
            # Check if domain is throttled (reduced rate)
            rate = self._per_domain_rate
            if domain in self._throttled_domains:
                rate = max(1, rate // 4)  # Reduce to 25% on throttle
            self._domain_buckets[domain] = TokenBucket(
                capacity=float(rate),
                tokens=float(rate),
                refill_rate=float(rate),
                last_refill=time.monotonic(),
            )
        return self._domain_buckets[domain]

    def _refill(self, bucket: TokenBucket) -> None:
        """Refill tokens based on elapsed time."""
        now = time.monotonic()
        elapsed = now - bucket.last_refill
        bucket.tokens = min(
            bucket.capacity,
            bucket.tokens + elapsed * bucket.refill_rate,
        )
        bucket.last_refill = now

    async def acquire(self, domain: str, timeout: float = 10.0) -> bool:
        """
        Acquire permission to make a request.
        Returns True if allowed, False if timed out.
        """
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            throttle_wait = 0.0

            async with self._lock:
                # Check if domain is currently throttled
                if domain in self._throttled_domains:
                    if time.monotonic() < self._throttled_domains[domain]:
                        throttle_wait = self._throttled_domains[domain] - time.monotonic()
                        if throttle_wait > timeout:
                            return False
                    else:
                        del self._throttled_domains[domain]

                if not throttle_wait:
                    # Refill both buckets
                    self._refill(self._global_bucket)
                    domain_bucket = self._get_domain_bucket(domain)
                    self._refill(domain_bucket)

                    # Try to consume from both buckets
                    if self._global_bucket.tokens >= 1.0 and domain_bucket.tokens >= 1.0:
                        self._global_bucket.tokens -= 1.0
                        domain_bucket.tokens -= 1.0
                        return True

            # Lock released. While throttled, sleep out the backoff rather than
            # falling through to the buckets — otherwise 429/503 backoff is a no-op.
            remaining = deadline - time.monotonic()
            await asyncio.sleep(max(0.0, min(throttle_wait or 0.05, remaining)))

        logger.warning(f"Rate limit timeout for domain: {domain}")
        return False

    async def record_throttled(self, domain: str, backoff_seconds: float = 30.0) -> None:
        """Record that a domain returned 429/503 — apply temporary throttle."""
        async with self._lock:
            throttle_until = time.monotonic() + backoff_seconds
            self._throttled_domains[domain] = throttle_until
            # Reduce the domain bucket capacity
            if domain in self._domain_buckets:
                bucket = self._domain_buckets[domain]
                bucket.refill_rate = max(0.5, bucket.refill_rate / 2)
                bucket.capacity = max(1.0, bucket.capacity / 2)
            logger.info(
                f"Domain {domain} throttled for {backoff_seconds}s (429/503 response)"
            )

    async def get_stats(self) -> dict:
        """Get rate limiter statistics."""
        async with self._lock:
            return {
                "global_tokens": round(self._global_bucket.tokens, 2),
                "global_capacity": self._global_bucket.capacity,
                "domain_count": len(self._domain_buckets),
                "throttled_domains": list(self._throttled_domains.keys()),
            }
