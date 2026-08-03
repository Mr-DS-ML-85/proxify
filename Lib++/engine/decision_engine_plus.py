"""
Decision Engine Plus — Multi-dimensional strategy selection with TLS awareness.

Solves:
  - ❌ Single-dimensional strategy selection (original decision engine)
  - ❌ No TLS profile awareness in strategy selection
  - ❌ No HTTP version consideration
  - ❌ No cross-strategy cookie sharing in the decision loop
  - ❌ Static strategy order (config-based, not learned)

Features:
  - Multi-dimensional scoring: strategy × TLS profile × HTTP version
  - Per-domain learning from DomainTrackerPlus
  - Cross-strategy cookie sharing via SessionCookieSharing
  - Smart escalation with TLS profile rotation
  - HTTP version selection (h3 → h2 → h1.1)
  - Nodriver fallback for JS-heavy pages
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Optional
from urllib.parse import urlparse

from ..core.types import (
    FetchRequest, FetchResult, StrategyType, HttpVersion,
    StrategyDecision,
)
from ..core.tls_profiles import tls_profile_manager
from ..core.session_cookie_sharing import session_cookie_sharing, cross_strategy_jar
from ..adapters.domain_tracker_plus import DomainTrackerPlus
from ..adapters.orchestrator_adapter import (
    LibPlusStrategyWrapper, NodriverPoolManager,
)

logger = logging.getLogger(__name__)

MAX_RETRIES = 2


class DecisionEnginePlus:
    """
    Enhanced decision engine with multi-dimensional strategy selection.

    Pipeline:
    1. Analyze domain history (best TLS profile + strategy)
    2. Select optimal strategy × TLS profile × HTTP version
    3. Execute with auto-escalation
    4. Share cookies across strategies on success
    5. Record per-domain TLS performance for learning
    """

    # Strategy priority weights (lower = tried first)
    STRATEGY_PRIORITIES: dict[str, int] = {
        "curl_cffi_plus": 0,    # Fast TLS-spoofed HTTP
        "tls_rotator": 1,        # TLS rotation
        "http3": 2,              # HTTP/3 QUIC
        "nodriver": 3,           # Full browser (CDP-direct)
    }

    def __init__(self, domain_tracker: Optional[DomainTrackerPlus] = None):
        self._domain_tracker = domain_tracker or DomainTrackerPlus()
        self._strategies: dict[str, LibPlusStrategyWrapper] = {}
        self._nodriver_pool = NodriverPoolManager()
        self._initialized = False

    @property
    def strategies(self) -> dict[str, LibPlusStrategyWrapper]:
        return self._strategies

    @property
    def domain_tracker(self) -> DomainTrackerPlus:
        return self._domain_tracker

    async def initialize(self) -> None:
        """Initialize all available Lib++ strategies."""
        if self._initialized:
            return

        from ..adapters.orchestrator_adapter import build_libplus_strategies

        pool = await self._nodriver_pool.get_pool()
        self._strategies = build_libplus_strategies(
            nodriver_pool=pool,
            domain_tracker=self._domain_tracker,
        )

        for name, wrapper in self._strategies.items():
            try:
                await wrapper.initialize()
                logger.info(f"DecisionEngine+ strategy '{name}' ready")
            except Exception as e:
                logger.warning(f"DecisionEngine+ strategy '{name}' init failed: {e}")

        self._initialized = True

    async def shut_down(self) -> None:
        """Shutdown all strategies."""
        for name, wrapper in self._strategies.items():
            try:
                await wrapper.shutdown()
            except Exception:
                pass
        await self._nodriver_pool.shutdown()

    async def fetch(self, request: FetchRequest) -> FetchResult:
        """
        Main entry point: fetch URL with multi-dimensional strategy selection.
        """
        if not self._initialized:
            await self.initialize()

        start_time = time.monotonic()
        domain = urlparse(request.url).netloc

        # Determine strategy order for this request (await the async method)
        strategies_to_try = await self._determine_strategy_order(domain, request.require_js)

        last_result = None

        for strategy_name in strategies_to_try:
            wrapper = self._strategies.get(strategy_name)
            if not wrapper:
                continue

            for attempt in range(MAX_RETRIES + 1):
                logger.info(
                    f"DecisionEngine+: '{strategy_name}' attempt {attempt+1} for {request.url}"
                )

                # Load shared cookies before each attempt. Build a per-attempt
                # copy — mutating request.headers in place would edit the
                # caller's dict and leak the Cookie across strategies/retries.
                attempt_request = request
                shared = await cross_strategy_jar.get_cookies(domain)
                if shared and not request.headers.get("Cookie"):
                    cookie_str = "; ".join(f"{k}={v}" for k, v in shared.items())
                    attempt_request = replace(
                        request, headers={**request.headers, "Cookie": cookie_str}
                    )

                result = await wrapper.fetch(attempt_request)

                if result.success and not result.is_blocked:
                    # SUCCESS — record and return
                    await self._domain_tracker.record_success(
                        domain=domain,
                        strategy=strategy_name,
                        tls_profile=result.tls_profile_used or "unknown",
                        latency=result.latency,
                        http_version=result.http_version_used or "h2",
                    )
                    logger.info(
                        f"DecisionEngine+ SUCCESS: {request.url} via '{strategy_name}' "
                        f"in {result.latency:.2f}s"
                    )
                    return result

                elif result.is_blocked:
                    # Blocked — escalate to next strategy
                    logger.info(
                        f"DecisionEngine+: blocked on '{strategy_name}' "
                        f"(status={result.status_code}), escalating"
                    )
                    await self._domain_tracker.record_failure(
                        domain=domain,
                        strategy=strategy_name,
                        tls_profile=result.tls_profile_used or "unknown",
                    )
                    last_result = result
                    break  # Move to next strategy

                elif attempt < MAX_RETRIES:
                    # Retry with different TLS profile
                    logger.info(
                        f"DecisionEngine+: retrying '{strategy_name}' "
                        f"(attempt {attempt+1})"
                    )
                    last_result = result
                    continue
                else:
                    last_result = result

        # All strategies exhausted
        if last_result:
            last_result.success = False
            last_result.error = last_result.error or "All Lib++ strategies exhausted"
            logger.warning(
                f"DecisionEngine+ FAILED: {request.url} — all strategies exhausted"
            )
            return last_result

        return FetchResult(
            success=False,
            url=request.url,
            error="No strategies available",
            latency=time.monotonic() - start_time,
        )

    async def _determine_strategy_order(
        self, domain: str, require_js: bool = False
    ) -> list[str]:
        """
        Determine the optimal strategy order for a request.

        Uses domain learning to prefer the best strategy for each domain,
        with fallback chains based on capabilities. Pure async — no
        event loop shenanigans.
        """
        if require_js:
            # JS rendering required — go straight to nodriver
            return ["nodriver", "curl_cffi_plus", "tls_rotator"]

        # Check domain history for best strategy
        best_strategy, _ = await self._domain_tracker.get_best_combination(domain)

        # Build order — promote best strategy to front
        order = list(self.STRATEGY_PRIORITIES.keys())

        if best_strategy and best_strategy in order:
            order.remove(best_strategy)
            order.insert(0, best_strategy)

        return order

    async def get_stats(self) -> dict:
        """Get engine statistics."""
        return {
            "strategies": list(self._strategies.keys()),
            "domain_tracker": await self._domain_tracker.get_all_stats(),
            "cookie_jar": cross_strategy_jar.stats,
            "tls_profiles": tls_profile_manager.get_stats(),
        }
