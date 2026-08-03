"""
Decision Engine — Core brain of the Stealth Scraper Orchestrator.
Multi-tier deterministic escalation with anti-bot detection and quality gates.

Pipeline (10-tier — all strategies kept for maximum fallback):
  curl_cffi_plus → simple → drissionpage_plus → nodriver →
  tls_rotator → scrapling → flaresolverr → playwright →
  puppeteer → puppeteer_plus

Domain memory reorders strategies per known domain (e.g., Reddit → playwright first),
but ALL 10 strategies remain available as fallbacks.
"""

import asyncio
import logging
import os
import sys
import time
from typing import Optional
from urllib.parse import urlparse

from config import config
from strategies.base import BaseStrategy, FetchRequest, FetchResult
from strategies.simple import SimpleStrategy
from strategies.scrapling_strategy import ScraplingStrategy
from strategies.flaresolverr_strategy import FlareSolverrStrategy
from strategies.playwright_strategy import PlaywrightStrategy
from strategies.puppeteer_strategy import PuppeteerStrategy
from strategies.gui_chrome import GuiChromeStrategy
from services.cache import L1Cache
from services.redis_cache import RedisCache
from services.circuit_breaker import CircuitBreaker
from services.cookie_jar import CookieManager
from services.proxy_manager import ProxyManager
from services.rate_limiter import RateLimiter
from services.session_manager import SessionManager
from services.metrics import metrics
from engine.domain_tracker import DomainTracker
from engine.request_dedup import RequestDedup
from engine.antibot import detect_antibot, decide_next_step
from engine.quality import score_quality
from engine.js_challenge_solver import is_js_challenge, is_captcha_page, solve_js_challenge
from engine.captcha_solver_bridge import solve_captcha_from_html
from engine.dom_cleaner import clean_dom, make_google_results_markdown

# RIL — Retrieval Intelligence Layer (all 8 brains, learns retrieval behavior)
try:
    import sys
    import os
    _ril_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.pardir)
    sys.path.insert(0, os.path.abspath(_ril_path))
    from ril import ril as _ril_instance
    RIL_AVAILABLE = True
except ImportError:
    _ril_instance = None
    RIL_AVAILABLE = False
except Exception:
    _ril_instance = None
    RIL_AVAILABLE = False

# Lib++ adapter — optional, provides advanced strategies (curl_cffi_plus, nodriver, tls_rotator, etc.)
# Import wrapped in try/except because the Lib++ package may not be installed
try:
    from Lib_plus_plus.adapters.orchestrator_adapter import LibPlusAdapter
except ImportError:
    try:
        from Lib__.adapters.orchestrator_adapter import LibPlusAdapter
    except ImportError:
        try:
            import importlib
            # Wildcard fallback: search for any 'Lib' directory with adapters.
            # Lib++ is a SIBLING of proxy-orchestrator — scan both the parent
            # of this file (proxy-orchestrator/) and its grandparent (the repo
            # root or proxify/), so the layout survives a repo reorg.
            import os, sys
            _scan = [
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            ]
            _lib_dirs = []
            for _base in _scan:
                if not os.path.isdir(_base):
                    continue
                for _d in os.listdir(_base):
                    if _d.startswith('Lib') and os.path.isdir(os.path.join(_base, _d)):
                        _lib_dirs.append(os.path.join(_base, _d))
            if _lib_dirs:
                for _p in _scan:
                    if _p not in sys.path:
                        sys.path.insert(0, _p)
                LibPlusAdapter = getattr(__import__(f'{os.path.basename(_lib_dirs[0])}.adapters.orchestrator_adapter', fromlist=['LibPlusAdapter']), 'LibPlusAdapter')
            else:
                LibPlusAdapter = None
        except Exception:
            LibPlusAdapter = None

logger = logging.getLogger(__name__)

MAX_RETRIES_PER_LAYER = 2


class DecisionEngine:
    """
    The Decision & Adaptive Engine — Multi-tier escalation.

    Pipeline:
    1. Cache lookup (L1 → L2)
    2. Rate limit check
    3. Request deduplication        4. Execute with 10-tier smart escalation (curl_cffi_plus → simple → drissionpage_plus → nodriver → tls_rotator → scrapling → flaresolverr → playwright → puppeteer → puppeteer_plus)
    5. Anti-bot detection on each response
    6. Quality gate before accepting
    7. Cache successful results
    """

    def __init__(self) -> None:
        # Core services
        self._cookie_manager = CookieManager()
        self._l1_cache = L1Cache()
        self._l2_cache = RedisCache()
        self._circuit_breaker = CircuitBreaker()
        self._proxy_manager = ProxyManager()
        self._rate_limiter = RateLimiter()
        self._session_manager = SessionManager(self._cookie_manager)
        self._domain_tracker = DomainTracker()
        self._dedup = RequestDedup()
        self._background_tasks: list[asyncio.Task] = []

        # Strategies (ordered by priority — tried in this order)
        self._strategies: dict[str, BaseStrategy] = {}
        self._strategy_order: list[str] = config.STRATEGY_ORDER
        self._initialized = False

        # RIL — Retrieval Intelligence Layer (8 brains for self-optimizing retrieval)
        self._ril = _ril_instance if RIL_AVAILABLE else None
        self._ril_available = RIL_AVAILABLE

        # Lib++ adapter (next-gen strategies)
        self._libplus: Optional[LibPlusAdapter] = None

        # Cookie file tracking (real user session cookies, Netscape format)
        self._cookie_file_mtime: float = 0.0

    async def initialize(self) -> None:
        """Initialize all strategies and services."""
        if self._initialized:
            return

        # Initialize all available core strategies
        strategy_classes = {
            "simple": SimpleStrategy(self._cookie_manager),
            "scrapling": ScraplingStrategy(self._cookie_manager),
            "flaresolverr": FlareSolverrStrategy(self._cookie_manager),
            "playwright": PlaywrightStrategy(self._cookie_manager),
            "puppeteer": PuppeteerStrategy(self._cookie_manager),
            "gui_chrome": GuiChromeStrategy(self._cookie_manager),
        }

        for name, strategy in strategy_classes.items():
            if name in self._strategy_order:
                try:
                    await strategy.initialize()
                    self._strategies[name] = strategy
                except Exception as e:
                    logger.warning(f"Strategy '{name}' init failed: {e}")

        # Initialize ALL Lib++ strategies (all kept for maximum fallback)
        if LibPlusAdapter is not None:
            try:
                self._libplus = LibPlusAdapter(
                    enable_curl_cffi_plus=config.CURL_CFFI_PLUS_ENABLED,
                    enable_nodriver=config.NODRIVER_ENABLED,
                    enable_tls_rotator=config.TLS_ROTATOR_ENABLED,
                    enable_puppeteer_plus=config.PUPPETEER_PLUS_ENABLED,
                    enable_drissionpage_plus=config.DRISSIONPAGE_PLUS_ENABLED,
                    nodriver_pool_size=config.NODRIVER_POOL_SIZE,
                )
                await self._libplus.initialize()
                for name, wrapper in self._libplus.strategies.items():
                    if name in self._strategy_order:
                        self._strategies[name] = wrapper  # type: ignore[assignment]
                        logger.info(f"Lib++ strategy '{name}' registered")
            except Exception as e:
                logger.warning(f"Lib++ adapter init failed (non-fatal): {e}")
        else:
            logger.debug("Lib++ adapter not available — advanced strategies skipped")


        # Bridge cross-strategy cookie jar to existing CookieManager (Lib++ optional)
        try:
            if LibPlusAdapter is not None:
                import importlib
                # Try multiple import paths for the cookie jar bridge
                for bridge_mod in ['Lib++.core.session_cookie_sharing',
                                   'Lib_plus_plus.core.session_cookie_sharing',
                                   'Lib__.core.session_cookie_sharing']:
                    try:
                        bridge_mod_obj = importlib.import_module(bridge_mod)
                        bridge_func = getattr(bridge_mod_obj, 'bridge_to_external_cookie_manager')
                        bridge_func(self._cookie_manager)
                        logger.info(f"Cross-strategy cookie jar bridged via {bridge_mod}")
                        break
                    except (ImportError, AttributeError):
                        continue
        except Exception as e:
            logger.debug(f"Cookie jar bridge skipped (Lib++ not available): {e}")

        # Warm RIL with benchmarked domain memory
        if self._ril_available and self._ril is not None:
            import time as _time
            _warmed = _time.monotonic()
            try:
                await self._ril.warm({
                    "old.reddit.com": {"best_strategy": "playwright", "best_tls": "chrome136", "avg_latency_ms": 2000, "total_requests": 10},
                    "www.reddit.com": {"best_strategy": "playwright", "best_tls": "chrome136", "avg_latency_ms": 1500, "total_requests": 10},
                    "reddit.com": {"best_strategy": "playwright", "best_tls": "chrome136", "avg_latency_ms": 2000, "total_requests": 10},
                    "www.google.com": {"best_strategy": "playwright", "best_tls": "chrome131", "avg_latency_ms": 11000, "total_requests": 10},
                    "google.com": {"best_strategy": "playwright", "best_tls": "chrome131", "avg_latency_ms": 11000, "total_requests": 10},
                    "discord.com": {"best_strategy": "tls_rotator", "avg_latency_ms": 110, "total_requests": 5},
                    "tiktok.com": {"best_strategy": "curl_cffi_plus", "avg_latency_ms": 550, "total_requests": 5},
                    "facebook.com": {"best_strategy": "playwright", "avg_latency_ms": 3000, "total_requests": 5},
                })
                logger.info(f"🧠 RIL warmed from benchmarks in {(_time.monotonic()-_warmed)*1000:.0f}ms")
            except Exception as e:
                logger.debug(f"RIL warm skipped: {e}")

        # Import the user's real session cookies (Netscape format exported by
        # scripts/brave_cookies.py) into the central jar. Every strategy
        # (simple/curl_cffi_plus HTTP, Lib++ bridge, GUI Chrome seed) picks
        # these up — the strongest trust signal for Google/Reddit anti-bots.
        try:
            imported = await self._cookie_manager.import_netscape_file(
                config.COOKIE_FILE
            )
            if imported:
                logger.info(f"Loaded {imported} real session cookies from {config.COOKIE_FILE}")
            if config.COOKIE_FILE and os.path.exists(config.COOKIE_FILE):
                # Record mtime so the refresh loop only re-imports on change
                self._cookie_file_mtime = os.path.getmtime(config.COOKIE_FILE)
        except Exception as e:
            logger.debug(f"Cookie file import failed (non-fatal): {e}")

        # Start background tasks. Handles are retained so shutdown() can cancel
        # them, and so they aren't garbage-collected mid-flight.
        self._background_tasks = [
            asyncio.create_task(self._session_cleanup_loop()),
            asyncio.create_task(self._cache_cleanup_loop()),
            asyncio.create_task(self._cookie_refresh_loop()),
        ]

        if self._proxy_manager.has_proxies:
            self._background_tasks.append(asyncio.create_task(self._proxy_health_loop()))

        self._initialized = True
        logger.info(
            f"DecisionEngine initialized with strategies: {list(self._strategies.keys())}"
        )

    async def fetch(self, request: FetchRequest) -> FetchResult:
        """
        Main entry point: fetch a URL with adaptive strategy selection.

        Flow:
        1. Check L1 cache → L2 cache
        2. Rate limit check
        3. Request deduplication
        4. Select strategy (auto or forced)
        5. Execute with 10-tier fallback escalation (curl_cffi_plus → simple → drissionpage_plus → nodriver → tls_rotator → scrapling → flaresolverr → playwright → puppeteer → puppeteer_plus) + antibot + quality
        6. Cache result
        7. Record metrics
        """
        if not self._initialized:
            await self.initialize()

        domain = urlparse(request.url).netloc
        start_time = time.monotonic()
        logger.info(f"DE fetch start: {request.url[:60]}")
        cache_key = L1Cache.make_key(request.method, request.url, request.params, request.session_id)

        # Wrap entire fetch in a timeout to prevent indefinite hangs
        try:
            return await asyncio.wait_for(
                self._fetch_impl(request, domain, start_time, cache_key),
                timeout=request.timeout + 30,
            )
        except asyncio.TimeoutError:
            return FetchResult(
                success=False, url=request.url,
                error=f"Global fetch timeout", latency=time.monotonic() - start_time,
            )
        except Exception as e:
            return FetchResult(
                success=False, url=request.url,
                error=f"Internal error: {e}", latency=time.monotonic() - start_time,
            )

    async def _fetch_impl(self, request: FetchRequest, domain: str, start_time: float, cache_key: str) -> FetchResult:
        # === Step 1: Cache lookup ===
        should_cache = config.CACHE_ENABLED and not request.bypass_cache and request.method.upper() == "GET"
        if should_cache:
            cached_dict = await self._check_cache(cache_key)
            if cached_dict:
                logger.debug(f"Cache HIT for {request.url}")
                cached_result = FetchResult.from_dict(cached_dict)
                cached_result.cached = True
                cached_result.strategy_used = "cache"
                cached_result.latency = time.monotonic() - start_time
                return cached_result
            metrics.record_cache_miss("combined")

        # === Step 2: Rate limiting ===
        try:
            acquired = await asyncio.wait_for(
                self._rate_limiter.acquire(domain), timeout=15
            )
            if not acquired:
                logger.warning(f"Rate limited: {domain}")
                return FetchResult(
                    success=False, url=request.url,
                    status_code=429, error="Rate limited",
                    latency=time.monotonic() - start_time,
                )
        except asyncio.TimeoutError:
            logger.warning(f"Rate limit acquire timed out: {domain}")
            return FetchResult(
                success=False, url=request.url,
                status_code=429, error="Rate limit timed out",
                latency=time.monotonic() - start_time,
            )

        # === Step 3: Session management ===
        if "google." in domain and "/search" in request.url:
            request.force_new_session = True
        await self._apply_session(request, domain)

        # === Step 4: Proxy assignment ===
        if not request.proxy_url and self._proxy_manager.has_proxies:
            request.proxy_url = await self._proxy_manager.get_proxy(domain)

        # === Step 5: Request deduplication ===
        dedup_key = f"{request.method}:{request.url}:{request.session_id or domain}"
        future, is_new = await self._dedup.get_or_create(dedup_key)

        if not is_new:
            try:
                return await asyncio.wait_for(future, timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        try:
            # === Step 6: Execute with smart escalation ===
            result = await self._execute_smart_pipeline(request, domain, start_time)

            # === Step 7: Cache successful results ===
            if should_cache and result.success:
                await self._store_cache(cache_key, result.to_dict())

            # Record overall metrics
            metrics.record_request(
                method=request.method,
                domain=domain,
                strategy=result.strategy_used,
                status_code=result.status_code,
                latency=result.latency,
            )

            if not future.done():
                future.set_result(result)

            return result

        except Exception as e:
            logger.error(f"DecisionEngine error: {e}")
            error_result = FetchResult(
                success=False, url=request.url,
                error=str(e), latency=time.monotonic() - start_time,
            )
            if not future.done():
                future.set_result(error_result)
            return error_result

    async def _execute_smart_pipeline(
        self, request: FetchRequest, domain: str,
        start_time: float = 0.0,
    ) -> FetchResult:
        """
        Execute the 10-tier smart fetch pipeline.

        For each strategy:
        1. Execute fetch
        2. Run antibot detection
        3. Run quality filter
        4. Decide: accept / retry / escalate
        """
        # Determine strategy order — RIL-driven
        skip_strategies = set()
        ril_sources = []
        
        if request.force_strategy:
            strategies_to_try = [request.force_strategy]
        else:
            strategies_to_try = list(self._strategy_order)
            
            # 1. RIL: Get optimal strategy from the 8 brains
            if self._ril_available and self._ril is not None:
                try:
                    ril_plan = await self._ril.get_optimal_strategy(request.url)
                    ril_sources = ril_plan.get("sources", [])
                    skip_strategies = set(ril_plan.get("skip_strategies", []))
                    
                    recommended = ril_plan.get("recommended_strategy", "")
                    confidence = ril_plan.get("confidence", 0.0)
                    
                    if recommended and confidence > 0.3 and recommended in self._strategies:
                        logger.info(
                            f"🧠 RIL recommends '{recommended}' for {domain} "
                            f"(confidence={confidence}, sources={ril_sources})"
                        )
                        strategies_to_try = [recommended] + [
                            s for s in self._strategy_order if s != recommended
                        ]
                    elif ril_sources:
                        logger.debug(f"🧠 RIL: low confidence ({confidence}) for {domain}, using defaults")
                except Exception as e:
                    logger.debug(f"RIL strategy selection error: {e}")
            
            # 2. Fallback: domain_tracker for recent learned data
            if not ril_sources:
                best = await self._domain_tracker.get_best_strategy(domain)
                if best and best in self._strategies:
                    strategies_to_try = [best] + [
                        s for s in self._strategy_order if s != best
                    ]

        # 3. Filter out strategies that RIL's Failure Brain says to skip
        if skip_strategies:
            filtered = [s for s in strategies_to_try if s not in skip_strategies]
            logger.info(f"🧠 RIL Failure Brain: skipping {skip_strategies - set(strategies_to_try)} for {domain}")
            if filtered:
                strategies_to_try = filtered

        last_result = None

        js_capable_strategies = {"nodriver", "playwright", "puppeteer", "puppeteer_plus", "drissionpage_plus"}

        # 4. JS-heavy domains MUST be fetched with a real browser — non-JS
        #    strategies return the SPA shell (near-empty markdown), which looks
        #    like success but is useless for LLM extraction. Prefer JS strategies.
        #
        #    NOTE: old.reddit.com is the CLASSIC server-rendered interface — it
        #    must NOT be routed to browsers first. Real browsers hit Reddit's
        #    reCAPTCHA on old.reddit, while the fast TLS-impersonating HTTP
        #    strategies (curl_cffi_plus with Safari fingerprint) get served.
        if not request.force_strategy:
            _is_old_reddit = "old.reddit.com" in domain
            _js_heavy = (
                ("reddit.com" in domain and not _is_old_reddit),
                "twitter.com" in domain, "x.com" in domain,
                "instagram.com" in domain, "facebook.com" in domain,
                "linkedin.com" in domain, "tiktok.com" in domain,
                "youtube.com" in domain, "cloud.furylogic.com" in domain,
            )
            if any(_js_heavy):
                js_first = [s for s in strategies_to_try if s in js_capable_strategies]
                rest = [s for s in strategies_to_try if s not in js_capable_strategies]
                if js_first:
                    strategies_to_try = js_first + rest
                    logger.info(
                        f"JS-heavy domain {domain}: reordered pipeline, "
                        f"JS strategies first: {strategies_to_try[:3]}..."
                    )
            elif _is_old_reddit:
                # old.reddit is server-rendered: fast HTTP/TLS strategies first
                http_first = [s for s in strategies_to_try if s not in js_capable_strategies]
                if http_first:
                    strategies_to_try = http_first + [
                        s for s in strategies_to_try if s in js_capable_strategies
                    ]
                    logger.info(
                        f"old.reddit server-rendered: HTTP strategies first: "
                        f"{strategies_to_try[:3]}..."
                    )

        for strategy_name in strategies_to_try:
            strategy = self._strategies.get(strategy_name)
            if strategy is None:
                logger.debug(f"Pipeline: '{strategy_name}' not in registered strategies, skipping")
                continue

            # Check circuit breaker
            can_run = await self._circuit_breaker.can_execute(strategy_name, domain)
            if not can_run:
                logger.debug(f"Circuit open for {strategy_name}:{domain}, skipping")
                metrics.record_strategy(strategy_name, "circuit_open")
                continue

            # Retry loop for this strategy layer
            for attempt in range(MAX_RETRIES_PER_LAYER + 1):
                logger.info(
                    f"Pipeline: '{strategy_name}' attempt {attempt+1} for {request.url}"
                )

                # Execute fetch (with exception safety — escalate on failure)
                try:
                    result = await strategy.fetch(request)
                except Exception as e:
                    logger.warning(
                        f"Pipeline: '{strategy_name}' attempt {attempt+1} "
                        f"threw exception: {e}"
                    )
                    await self._circuit_breaker.record_failure(strategy_name, domain)
                    metrics.record_strategy(strategy_name, "exception")
                    last_result = FetchResult(
                        success=False, url=request.url, error=str(e),
                        latency=time.monotonic() - start_time,
                    )
                    break  # Don't retry, escalate to next strategy
                result.retries = attempt

                # --- Anti-bot detection (includes TLS/CDN fingerprint awareness) ---
                antibot = detect_antibot(
                    html=result.html,
                    status_code=result.status_code,
                    final_url=result.final_url,
                    content_length=len(result.html) if result.html else 0,
                    response_headers=result.headers,
                )
                result.antibot_score = antibot.score
                result.metadata["antibot_status"] = antibot.status
                result.metadata["antibot_reasons"] = antibot.reasons

                # --- Quality filter ---
                quality = score_quality(result.html)
                result.quality_score = quality.quality_score
                result.metadata["quality_usable"] = quality.usable

                # --- JS Challenge / Captcha detection ---
                # Two-tier approach:
                #   1. If captcha detected on a JS-capable strategy (playwright),
                #      try solving via ai-captcha-bypass vision API
                #   2. If that fails, natural strategy escalation handles it
                #      (domain memory already picks JS for known challenging domains)
                is_challenge = result.html and (
                    is_js_challenge(result.html) or
                    is_captcha_page(result.html)
                )

                if is_challenge:
                    ctype = "PoW/JS" if is_js_challenge(result.html) else "captcha"
                    # Nested challenge-solve fetches (see js_challenge_solver.py)
                    # carry a marker so we never recursively re-solve.
                    nested_solve = (request.headers or {}).get(
                        "X-Orchestrator-Challenge-Solve"
                    ) == "skip"
                    logger.info(
                        f"{ctype} challenge detected on '{strategy_name}' for {request.url}"
                    )
                    result.metadata["challenge_detected"] = True
                    result.metadata["challenge_type"] = ctype.lower()

                    # Fast-escalate: non-JS strategies can't solve this
                    if strategy_name not in js_capable_strategies:
                        last_result = result
                        await self._circuit_breaker.record_failure(strategy_name, domain)
                        metrics.record_strategy(strategy_name, f"{ctype.lower()}_escalated")
                        break

                    # Captcha solving via ai-captcha-bypass (no sub-requests!)
                    # Only attempt on JS-capable strategies (playwright).
                    # Skipped on nested challenge-solve fetches (recursion guard).
                    if not nested_solve and ctype == "captcha" and strategy_name in js_capable_strategies:
                        try:
                            captcha_result = await solve_captcha_from_html(
                                url=request.url,
                                html=result.html or "",
                                playwright_strategy=strategy,
                            )
                            if captcha_result.get("solved"):
                                logger.info(
                                    f"Captcha solved via ai-captcha-bypass for {request.url}"
                                )
                                # If we got cookies, store them
                                captcha_cookies = captcha_result.get("cookies", {})
                                if captcha_cookies:
                                    await self._cookie_manager.set_cookies(
                                        domain, captcha_cookies
                                    )
                                # Accept the result — captcha is solved, use existing HTML
                                # If solved via interactive Playwright, use the new HTML
                                captcha_html = captcha_result.get("html", "")
                                if captcha_html and len(captcha_html) > len(result.html or ""):
                                    result.html = captcha_html
                                    logger.info(
                                        f"Using interactively solved content"
                                        f" ({len(captcha_html)} chars)"
                                    )
                            else:
                                logger.debug(
                                    f"Captcha solve failed for {request.url}: "
                                    f"{captcha_result.get('error', 'unknown')}"
                                )
                        except Exception as e:
                            logger.debug(f"Captcha bridge error: {e}")

                    # PoW/JS challenge solving: route through Playwright again
                    # (handles Reddit seed-doubling, Cloudflare challenge pages, etc.)
                    # Skipped on nested challenge-solve fetches (recursion guard).
                    if not nested_solve and ctype == "PoW/JS" and strategy_name in js_capable_strategies:
                        try:
                            logger.info(f"Solving PoW/JS challenge for {request.url} via re-fetch")
                            pw_result = await solve_js_challenge(request.url, result.html or "")
                            if pw_result and pw_result.solved:
                                logger.info(f"PoW/JS solved for {request.url}: {len(pw_result.html)} bytes")
                                result.html = pw_result.html
                                result.success = True
                                result.status_code = 200
                                result.final_url = pw_result.final_url or result.final_url
                                if pw_result.cookies:
                                    await self._cookie_manager.set_cookies(domain, pw_result.cookies)
                            else:
                                logger.warning(f"PoW/JS solve failed for {request.url}: {pw_result and pw_result.error}")
                        except Exception as e:
                            logger.debug(f"PoW/JS solve error: {e}")

                    # Google block solving: route through Playwright for JS execution
                    # Skipped on nested challenge-solve fetches (recursion guard).
                    if not nested_solve and ctype == "google_block" and strategy_name in js_capable_strategies:
                        try:
                            logger.info(f"Google block detected for {request.url}, re-fetching via Playwright")
                            pw_result = await solve_js_challenge(request.url, result.html or "")
                            if pw_result and pw_result.solved:
                                logger.info(f"Google block bypassed for {request.url}: {len(pw_result.html)} bytes")
                                result.html = pw_result.html
                                result.success = True
                                result.status_code = 200
                                result.final_url = pw_result.final_url or result.final_url
                                if pw_result.cookies:
                                    await self._cookie_manager.set_cookies(domain, pw_result.cookies)
                            else:
                                logger.warning(f"Google block bypass failed for {request.url}: {pw_result and pw_result.error}")
                        except Exception as e:
                            logger.debug(f"Google block bypass error: {e}")

                # --- Decide next step ---
                decision = decide_next_step(
                    score=antibot.score,
                    attempt=attempt,
                    current_method=strategy_name,
                    strategy_order=strategies_to_try,
                    max_retries_per_layer=MAX_RETRIES_PER_LAYER,
                )

                # ═══ Google post-processing ═══
                # Any Google search result that has real h3/serp content should
                # be force-accepted regardless of antibot score. The wrapper HTML
                # from FlareSolverr/Playwright/nodriver triggers antibot scoring
                # (~50), which causes false rejections.
                is_google_search = "google." in domain and "/search" in request.url
                
                if is_google_search:
                    # Check if result has real Google content via h3 or cleaned results
                    has_real_content = (
                        "<h3" in (result.html or "")
                        or (result.metadata.get("google_results")
                            and len(result.metadata["google_results"]) > 0)
                    )
                    if has_real_content and decision["action"] != "accept":
                        logger.info(
                            f"Google override: forcing accept for {request.url} "
                            f"(strategy={strategy_name}, antibot={antibot.score}, "
                            f"quality={quality.quality_score})"
                        )
                        decision["action"] = "accept"
                        result.metadata["google_force_accept"] = True
                    elif not has_real_content and decision["action"] == "accept":
                        # Google stub (enablejs shell / consent / 'trouble accessing'
                        # hidden div) scored low on antibot but contains ZERO results.
                        # Escalate to JS-capable strategies instead of returning it.
                        logger.info(
                            f"Google empty stub — escalating for {request.url} "
                            f"(strategy={strategy_name}, ab={antibot.score})"
                        )
                        result.metadata["google_empty_stub"] = True
                        decision = {
                            "action": "escalate",
                            "next_method": None,
                            "retry_count": 0,
                        }
                    
                    # If we have Google results metadata, add markdown to HTML
                    google_results = result.metadata.get("google_results", [])
                    if google_results:
                        md = make_google_results_markdown(google_results)
                        result.metadata["google_markdown"] = md

                # ── Quality gate: never accept unusable content ──
                # decide_next_step() only looks at the ANTIBOT score; a captcha
                # page or JS shell scores low on antibot but is useless. If
                # quality scoring says unusable, escalate instead of accepting.
                if (
                    decision["action"] == "accept"
                    and not quality.usable
                    and not result.metadata.get("google_force_accept")
                ):
                    logger.info(
                        f"Quality gate: q={quality.quality_score} unusable "
                        f"({quality.reasons}) on '{strategy_name}' — escalating {request.url}"
                    )
                    result.metadata["quality_gated"] = True
                    result.metadata["quality_reasons"] = quality.reasons
                    decision = {
                        "action": "escalate",
                        "next_method": None,
                        "retry_count": 0,
                    }

                # ── Challenge-unsolved gate: never accept an unsolved challenge ──
                # If the response still contains captcha/JS-challenge markers after
                # the solve attempts, escalate instead of returning it as content.
                if (
                    decision["action"] == "accept"
                    and is_challenge
                    and result.html
                    and (
                        is_js_challenge(result.html)
                        or is_captcha_page(result.html)
                    )
                ):
                    logger.info(
                        f"Challenge still present after '{strategy_name}' solve attempt "
                        f"for {request.url} — escalating"
                    )
                    result.metadata["challenge_unsolved"] = True
                    decision = {
                        "action": "escalate",
                        "next_method": None,
                        "retry_count": 0,
                    }

                logger.info(
                    f"Pipeline decision: antibot={antibot.score} quality={quality.quality_score} "
                    f"action={decision['action']} ({strategy_name})"
                )

                if decision["action"] == "accept":
                    # ── Final DOM cleaning for accepted results ──
                    # Clean the HTML: strip scripts, styles, extract content
                    if result.html and len(result.html) > 500:
                        cleaned = clean_dom(result.html, url=request.url)
                        if cleaned.success:
                            result.html = cleaned.clean_html
                            result.metadata["dom_cleaned"] = True
                            result.metadata["dom_clean_method"] = cleaned.method
                            result.metadata["word_count"] = cleaned.word_count
                        logger.debug(
                            f"DOM cleaner: {len(result.html) if result.html else 0} chars → "
                            f"{len(cleaned.clean_html) if cleaned.success else '?'} chars, "
                            f"{len(cleaned.google_results)} Google results"
                        )

                    # SUCCESS — record and return
                    await self._circuit_breaker.record_success(strategy_name, domain)
                    await self._domain_tracker.record_success(
                        domain, strategy_name, result.latency
                    )
                    metrics.record_strategy(strategy_name, "success")

                    # RIL: record success (updates Domain, Strategy, Fingerprint, Extraction, Freshness brains)
                    if self._ril_available and self._ril is not None:
                        try:
                            await self._ril.record_success(
                                url=request.url,
                                strategy=strategy_name,
                                latency_ms=result.latency * 1000,
                                content=result.html or "",
                                extractor="reader",
                                tls="chrome136",
                                headers="chrome136",
                            )
                        except Exception:
                            pass

                    if request.proxy_url:
                        await self._proxy_manager.record_success(
                            request.proxy_url, result.latency
                        )

                    logger.info(
                        f"Pipeline SUCCESS: {request.url} via '{strategy_name}' "
                        f"in {result.latency:.2f}s (ab={antibot.score} q={quality.quality_score})"
                    )
                    result.success = True
                    return result

                elif decision["action"] == "retry":
                    # Retry same strategy (rotate proxy/session)
                    logger.debug(f"Pipeline: retrying '{strategy_name}' (attempt {attempt+1})")
                    last_result = result

                    # Rotate proxy for retry
                    if self._proxy_manager.has_proxies:
                        if request.proxy_url:
                            await self._proxy_manager.record_failure(
                                request.proxy_url
                            )
                        request.proxy_url = await self._proxy_manager.get_proxy(domain)
                    continue

                elif decision["action"] == "escalate":
                    # Escalate to next strategy
                    logger.info(
                        f"Pipeline: escalating from '{strategy_name}' "
                        f"(ab={antibot.score})"
                    )
                    last_result = result

                    # RIL: record failure
                    if self._ril_available and self._ril is not None:
                        try:
                            await self._ril.record_failure(
                                url=request.url,
                                strategy=strategy_name,
                                reason=f"antibot_score_{antibot.score}"
                            )
                        except Exception:
                            pass

                    # Record failure for circuit breaker
                    await self._circuit_breaker.record_failure(strategy_name, domain)
                    metrics.record_strategy(strategy_name, "escalated")

                    if result.status_code in (429, 503):
                        await self._rate_limiter.record_throttled(domain)
                    break  # Move to next strategy

                else:  # "fail"
                    last_result = result
                    break

        # All strategies exhausted — record final failure to RIL
        if last_result:
            last_result.success = False
            last_result.error = last_result.error or "All strategies exhausted"
            logger.warning(
                f"Pipeline FAILED: {request.url} — all strategies exhausted "
                f"(last: ab={last_result.antibot_score} q={last_result.quality_score})"
            )
            
            # RIL: record final failure
            if self._ril_available and self._ril is not None:
                try:
                    await self._ril.record_failure(
                        url=request.url,
                        strategy=last_result.strategy_used or "pipeline_exhausted",
                        reason=f"all_strategies_exhausted_ab{last_result.antibot_score}"
                    )
                except Exception:
                    pass
            
            return last_result

        return FetchResult(
            success=False, url=request.url,
            error="No strategies available",
        )

    # === Session Management ===

    async def _apply_session(self, request: FetchRequest, domain: str) -> None:
        """Apply session settings to request."""
        session = await self._session_manager.get_or_create(
            domain, request.session_id, request.force_new_session
        )

        if session:
            request.session_id = session.session_id
            if hasattr(session, "metadata") and session.metadata:
                if "user_agent" in session.metadata:
                    request.headers.setdefault("User-Agent", session.metadata["user_agent"])
                if "proxy_url" in session.metadata and not request.proxy_url:
                    request.proxy_url = session.metadata["proxy_url"]

    # === Cache Operations ===

    async def _check_cache(self, key: str) -> Optional[dict]:
        """Check L1 then L2 cache."""
        result = await self._l1_cache.get(key)
        if result:
            metrics.record_cache_hit("l1")
            return result

        try:
            result = await self._l2_cache.get(key)
            if result:
                metrics.record_cache_hit("l2")
                await self._l1_cache.set(key, result)
                return result
        except Exception:
            pass

        return None

    async def _store_cache(self, key: str, data: dict) -> None:
        """Store in both L1 and L2 cache."""
        await self._l1_cache.set(key, data)
        try:
            await self._l2_cache.set(key, data)
        except Exception:
            pass

    # === Background Tasks ===

    async def _session_cleanup_loop(self) -> None:
        """Periodically clean up expired sessions."""
        while True:
            try:
                await asyncio.sleep(config.SESSION_CLEANUP_INTERVAL)
                await self._session_manager.cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Session cleanup error: {e}")

    async def _cache_cleanup_loop(self) -> None:
        """Periodically clean up expired L1 cache entries."""
        while True:
            try:
                await asyncio.sleep(60)
                await self._l1_cache.cleanup_expired()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Cache cleanup error: {e}")

    async def _proxy_health_loop(self) -> None:
        """Periodically check proxy health."""
        while True:
            try:
                await asyncio.sleep(config.PROXY_HEALTH_CHECK_INTERVAL)
                await self._proxy_manager.health_check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Proxy health check error: {e}")

    async def _cookie_refresh_loop(self) -> None:
        """Periodically re-import the cookie file if it changed on disk.

        Lets users drop a freshly-exported cookie file (scripts/brave_cookies.py)
        into the configured path and have the orchestrator pick it up without
        a restart — sessions stay warm without ever touching a GUI browser.
        """
        while True:
            try:
                await asyncio.sleep(config.COOKIE_REFRESH_INTERVAL)
                path = config.COOKIE_FILE
                if path and os.path.exists(path):
                    mtime = os.path.getmtime(path)
                    if mtime > self._cookie_file_mtime:
                        imported = await self._cookie_manager.import_netscape_file(path)
                        self._cookie_file_mtime = mtime
                        if imported:
                            logger.info(
                                f"Cookie refresh: re-imported {imported} cookies from {path}"
                            )
                        # Keep the GUI Chrome in sync — both paths must present
                        # the same cookie set or Google sees two identities.
                        await self._inject_cookies_into_gui()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Cookie refresh error: {e}")

    # === Public API ===

    async def _inject_cookies_into_gui(self) -> bool:
        """Best-effort: push the freshly imported cookie file into the GUI
        Chrome's persistent profile so the browser path and the HTTP path
        always present the SAME cookie set. Runs the proven injector script
        (brave_cookies.py inject) as a subprocess; non-fatal on failure."""
        path = config.COOKIE_FILE
        if not path or not os.path.exists(path):
            return False
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "scripts", "brave_cookies.py",
        )
        if not os.path.exists(script):
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, script, "inject",
                "--src", path, "--cdp", "http://127.0.0.1:9222",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rc = await asyncio.wait_for(proc.wait(), timeout=60)
            if rc == 0:
                logger.info("Auto cookie refresh: injected fresh cookies into GUI Chrome")
                return True
        except Exception as e:
            logger.debug(f"GUI cookie inject skipped (non-fatal): {e}")
        return False

    async def refresh_cookies(self, path: Optional[str] = None) -> dict:
        """Force a re-import of the real-session cookie file (API: POST /cookies/refresh).

        Re-imports into the central jar AND pushes the fresh cookies into the
        GUI Chrome profile so every path stays fingerprint-consistent.
        """
        path = path or config.COOKIE_FILE
        imported = await self._cookie_manager.import_netscape_file(path)
        if path and os.path.exists(path):
            self._cookie_file_mtime = os.path.getmtime(path)
        injected = await self._inject_cookies_into_gui()
        return {
            "imported": imported,
            "gui_chrome_injected": injected,
            **self._cookie_manager.status(),
        }

    async def cookie_status(self) -> dict:
        """Current cookie jar status (API: GET /cookies/status)."""
        return self._cookie_manager.status()

    async def get_stats(self) -> dict:
        """Get engine statistics."""
        libplus_stats = {}
        if self._libplus and self._libplus.domain_tracker:
            try:
                # Try multiple possible import paths for Lib++ domain tracker
                for mod_name in ['Lib++.adapters.domain_tracker_plus', 'Lib_plus_plus.adapters.domain_tracker_plus', 'Lib__.adapters.domain_tracker_plus']:
                    try:
                        mod = __import__(mod_name, fromlist=['DomainTrackerPlus'])
                        DomainTrackerPlus = getattr(mod, 'DomainTrackerPlus')
                        if isinstance(self._libplus.domain_tracker, DomainTrackerPlus):
                            libplus_stats = await self._libplus.domain_tracker.get_all_stats()
                        break
                    except (ImportError, AttributeError):
                        continue
            except Exception:
                pass

        return {
            "strategies": list(self._strategies.keys()),
            "strategy_order": self._strategy_order,
            "pipeline": "smart_8_tier_libplus",
            "max_retries_per_layer": MAX_RETRIES_PER_LAYER,
            "libplus": {
                "enabled": bool(libplus_stats),
                "domain_tracker_domains": len(libplus_stats),
            } if not libplus_stats else {
                "enabled": True,
                "domain_tracker_domains": len(libplus_stats),
                "domain_stats": libplus_stats,
            },
            "rate_limiter": await self._rate_limiter.get_stats(),
            "proxies": {
                "count": len(self._proxy_manager._proxies) if hasattr(self._proxy_manager, "_proxies") else 0,
                "has_proxies": self._proxy_manager.has_proxies,
            },
            "sessions": {
                "active": self._session_manager.active_count if hasattr(self._session_manager, "active_count") else 0,
            },
            "cache": {
                "l1_size": len(self._l1_cache._cache) if hasattr(self._l1_cache, "_cache") else 0,
            },
        }

    async def shutdown(self) -> None:
        """Gracefully shutdown all strategies and services."""
        logger.info("Shutting down DecisionEngine...")

        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks = []

        for name, strategy in self._strategies.items():
            try:
                await strategy.shutdown()
                logger.debug(f"Strategy '{name}' shut down")
            except Exception as e:
                logger.warning(f"Error shutting down '{name}': {e}")

        # Shutdown Lib++ adapter
        if self._libplus:
            try:
                await self._libplus.shutdown()
                logger.info("Lib++ adapter shut down")
            except Exception as e:
                logger.warning(f"Lib++ shutdown error: {e}")

        try:
            await self._l2_cache.close()
        except Exception as e:
            logger.warning(f"Redis close error: {e}")

        self._initialized = False
