"""
Orchestrator Adapter — Bridges Lib++ strategies to proxy-orchestrator's BaseStrategy pattern.

Allows the existing proxy-orchestrator to use Lib++ strategies seamlessly:
  - nodriver_strategy → proxy-orchestrator BaseStrategy
  - curl_cffi_plus → proxy-orchestrator BaseStrategy  
  - tls_rotator → proxy-orchestrator BaseStrategy
  - http3_client → proxy-orchestrator BaseStrategy

Solves:
  - ❌ nodriver not integrated into orchestrator
  - ❌ New strategies require modifying orchestration pipeline
  - ❌ Cross-strategy cookie/TLS sharing not possible
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from ..core.types import (
    FetchRequest as LibFetchRequest,
    FetchResult as LibFetchResult,
    BaseLibPlusStrategy,
)
from ..strategies.nodriver_strategy import NodriverStrategy, NodriverPool
from ..strategies.curl_cffi_plus import CurlCffiPlusStrategy
from ..strategies.tls_rotator import TlsRotator, TlsRotationEngine
from ..strategies.puppeteer_plus import PuppeteerPlusStrategy
from ..strategies.drissionpage_plus import DrissionPagePlusStrategy, DrissionPagePool
from ..core.http3_client import Http3Client
from ..core.session_cookie_sharing import cross_strategy_jar
from .domain_tracker_plus import DomainTrackerPlus

logger = logging.getLogger(__name__)


class LibPlusStrategyWrapper:
    """
    Wraps a Lib++ BaseLibPlusStrategy for use as a proxy-orchestrator BaseStrategy.

    Handles:
    - Type conversion (orchestrator FetchRequest ↔ Lib++ FetchRequest)
    - Name mapping
    - Priority assignment
    - Cookie sharing between strategies
    """

    def __init__(
        self,
        libplus_strategy: BaseLibPlusStrategy,
        priority: int = 15,
        cookie_jar: Any = None,
    ):
        self._strategy = libplus_strategy
        self._priority = priority
        self._cookie_jar = cookie_jar

    @property
    def name(self) -> str:
        return self._strategy.strategy_type.value

    @property
    def priority(self) -> int:
        return self._priority

    async def initialize(self) -> None:
        await self._strategy.initialize()

    async def shutdown(self) -> None:
        await self._strategy.shutdown()

    async def fetch(self, request: Any) -> Any:
        """Convert proxy-orchestrator request to Lib++ request and execute."""
        # proxy-orchestrator's package isn't necessarily on sys.path when Lib++
        # is used standalone — fall back to Lib++'s own result type.
        try:
            from strategies.base import FetchResult as OrchResult
        except ImportError:
            OrchResult = LibFetchResult

        # Convert proxy-orchestrator FetchRequest to Lib++ FetchRequest
        lib_request = LibFetchRequest(
            url=request.url,
            method=request.method,
            headers=request.headers,
            body=request.body,
            params=request.params,
            timeout=request.timeout,
            proxy_url=request.proxy_url,
            session_id=request.session_id,
            bypass_cache=request.bypass_cache,
            metadata=getattr(request, "metadata", {}),
        )

        # Execute via Lib++ strategy
        lib_result: LibFetchResult = await self._strategy.fetch(lib_request)

        # Convert back to proxy-orchestrator FetchResult
        return OrchResult(
            success=lib_result.success,
            status_code=lib_result.status_code,
            url=lib_result.url,
            final_url=lib_result.final_url,
            headers=lib_result.headers,
            html=lib_result.html,
            body=lib_result.body,
            cookies=lib_result.cookies,
            strategy_used=lib_result.strategy_used,
            latency=lib_result.latency,
            retries=lib_result.retries,
            error=lib_result.error,
            cached=lib_result.cached,
            antibot_score=lib_result.antibot_score,
            quality_score=lib_result.quality_score,
            metadata={
                **(lib_result.metadata or {}),
                "tls_profile_used": lib_result.tls_profile_used,
                "http_version_used": lib_result.http_version_used,
            },
        )


class NodriverPoolManager:
    """
    Manages the nodriver pool lifecycle for the orchestrator.

    This keeps a singleton nodriver pool that can be shared across
    multiple orchestrator instances.
    """

    def __init__(self, max_instances: int = 3):
        self._pool: Optional[NodriverPool] = None
        self._max_instances = max_instances

    async def get_pool(self) -> NodriverPool:
        if self._pool is None:
            self._pool = NodriverPool(max_instances=self._max_instances)
        return self._pool

    async def cleanup(self) -> None:
        if self._pool:
            await self._pool.cleanup()

    async def shutdown(self) -> None:
        if self._pool:
            await self._pool.shutdown()
            self._pool = None


def build_libplus_strategies(
    nodriver_pool: Optional[NodriverPool] = None,
    domain_tracker: Optional[DomainTrackerPlus] = None,
    config: Optional[dict[str, bool]] = None,
) -> dict[str, LibPlusStrategyWrapper]:
    """
    Build a dictionary of Lib++ strategy wrappers for the proxy-orchestrator.

    Args:
        nodriver_pool: Shared NodriverPool instance
        domain_tracker: DomainTrackerPlus for TLS learning
        config: Dict with keys like 'enable_nodriver', 'enable_http3', etc.

    Returns:
        dict[str, LibPlusStrategyWrapper]: Strategy name -> wrapper
    """
    if config is None:
        config = {
            "enable_nodriver": True,
            "enable_curl_cffi_plus": True,
            "enable_tls_rotator": True,
            "enable_puppeteer_plus": True,
            "enable_drissionpage_plus": True,
            "enable_http3": False,
        }

    strategies: dict[str, LibPlusStrategyWrapper] = {}

    if config.get("enable_curl_cffi_plus", True):
        cffi_plus = CurlCffiPlusStrategy()
        strategies["curl_cffi_plus"] = LibPlusStrategyWrapper(
            cffi_plus, priority=8,
        )

    if config.get("enable_nodriver", True):
        pool = nodriver_pool or NodriverPool()
        nodriver = NodriverStrategy(pool=pool)
        strategies["nodriver"] = LibPlusStrategyWrapper(
            nodriver, priority=12,
        )

    if config.get("enable_tls_rotator", True):
        engine = TlsRotationEngine()
        rotator = TlsRotator(rotation_engine=engine)
        strategies["tls_rotator"] = LibPlusStrategyWrapper(
            rotator, priority=18,
        )

    if config.get("enable_drissionpage_plus", True):
        dpp_pool = nodriver_pool  # Reuse same nodriver pool for consistency
        dpp = DrissionPagePlusStrategy(pool=DrissionPagePool(max_instances=2))
        strategies["drissionpage_plus"] = LibPlusStrategyWrapper(
            dpp, priority=16,
        )

    if config.get("enable_puppeteer_plus", True):
        pptr = PuppeteerPlusStrategy()
        strategies["puppeteer_plus"] = LibPlusStrategyWrapper(
            pptr, priority=22,
        )

    if config.get("enable_http3", False):
        h3 = Http3Client()
        strategies["http3"] = LibPlusStrategyWrapper(
            h3, priority=20,
        )

    return strategies


class LibPlusAdapter:
    """
    Main adapter that integrates Lib++ into the proxy-orchestrator ecosystem.

    Usage:
        adapter = LibPlusAdapter()
        await adapter.initialize()
        strategies = adapter.strategies  # dict[str, LibPlusStrategyWrapper]
    """

    def __init__(
        self,
        enable_nodriver: bool = True,
        enable_curl_cffi_plus: bool = True,
        enable_tls_rotator: bool = True,
        enable_puppeteer_plus: bool = True,
        enable_drissionpage_plus: bool = True,
        enable_http3: bool = False,
        nodriver_pool_size: int = 3,
    ):
        self._nodriver_pool = NodriverPoolManager(max_instances=nodriver_pool_size)
        self._domain_tracker = DomainTrackerPlus()
        self._strategies: dict[str, LibPlusStrategyWrapper] = {}
        self._config = {
            "enable_nodriver": enable_nodriver,
            "enable_curl_cffi_plus": enable_curl_cffi_plus,
            "enable_tls_rotator": enable_tls_rotator,
            "enable_puppeteer_plus": enable_puppeteer_plus,
            "enable_drissionpage_plus": enable_drissionpage_plus,
            "enable_http3": enable_http3,
        }

    @property
    def strategies(self) -> dict[str, LibPlusStrategyWrapper]:
        return self._strategies

    @property
    def domain_tracker(self) -> DomainTrackerPlus:
        return self._domain_tracker

    async def initialize(self) -> None:
        """Initialize all Lib++ strategies."""
        pool = await self._nodriver_pool.get_pool()
        self._strategies = build_libplus_strategies(
            nodriver_pool=pool,
            domain_tracker=self._domain_tracker,
            config=self._config,
        )
        for name, wrapper in self._strategies.items():
            await wrapper.initialize()
            logger.info(f"Lib++ strategy '{name}' initialized")

    async def shutdown(self) -> None:
        """Shutdown all Lib++ strategies."""
        for name, wrapper in self._strategies.items():
            try:
                await wrapper.shutdown()
            except Exception as e:
                logger.warning(f"Error shutting down Lib++ '{name}': {e}")
        await self._nodriver_pool.shutdown()
        logger.info("Lib++ adapter shut down")
