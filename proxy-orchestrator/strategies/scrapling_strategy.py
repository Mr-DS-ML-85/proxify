"""
Scrapling Strategy — Adaptive anti-bot scraping using Scrapling's StealthyFetcher.
Bypasses Cloudflare Turnstile, CDP leaks, WebRTC leaks, canvas fingerprinting.
"""

import logging
import time
from typing import Optional
from urllib.parse import urlparse

from strategies.base import BaseStrategy, FetchRequest, FetchResult
from services.cookie_jar import CookieManager
from config import config
from engine.dom_cleaner import clean_dom, make_google_results_markdown

logger = logging.getLogger(__name__)


class ScraplingStrategy(BaseStrategy):
    """
    Uses Scrapling's StealthyFetcher for advanced anti-bot evasion.

    Features (from Scrapling docs):
    - Bypasses Cloudflare Turnstile/Interstitial (solve_cloudflare=True)
    - Bypasses CDP runtime leaks and WebRTC leaks (block_webrtc=True)
    - Canvas noise generation to prevent fingerprinting (hide_canvas=True)
    - Patches headless mode detection
    - TLS fingerprint impersonation
    - Google search referer by default

    Falls back to DynamicFetcher for heavy JS rendering.
    Supports AsyncStealthySession for persistent browser sessions.
    """

    def __init__(self, cookie_manager: CookieManager) -> None:
        self._cookie_manager = cookie_manager
        self._available = config.SCRAPLING_ENABLED
        self._initialized = False

    @property
    def name(self) -> str:
        return "scrapling"

    @property
    def priority(self) -> int:
        return 20  # Second in escalation order

    async def initialize(self) -> None:
        """Check if Scrapling is available and install browser deps if needed."""
        if not self._available:
            logger.warning("Scrapling strategy disabled via config")
            return
        try:
            from scrapling.fetchers import StealthyFetcher
            self._initialized = True
            logger.info("ScraplingStrategy initialized (StealthyFetcher available)")
        except ImportError:
            logger.warning(
                "Scrapling not installed. Install with: pip install 'scrapling[fetchers]' && scrapling install"
            )
            self._available = False

    async def fetch(self, request: FetchRequest) -> FetchResult:
        """Fetch using Scrapling's StealthyFetcher with anti-bot evasion."""
        start_time = time.monotonic()

        if not self._available:
            return self._make_result(
                request, start_time,
                success=False,
                error="Scrapling not available",
            )

        if not self._initialized:
            await self.initialize()
            if not self._available:
                return self._make_result(
                    request, start_time,
                    success=False,
                    error="Scrapling initialization failed",
                )

        try:
            # Scrapling 0.2.x API: StealthyFetcher and DynamicFetcher
            # In 0.2.94+ some classes may be renamed — try both import paths
            try:
                from scrapling.fetchers import StealthyFetcher
            except ImportError:
                from scrapling import StealthyFetcher
            try:
                from scrapling.fetchers import DynamicFetcher
            except ImportError:
                try:
                    from scrapling import DynamicFetcher
                except ImportError:
                    DynamicFetcher = None

            # Build fetch kwargs from config and request
            fetch_kwargs = {
                "url": request.url,
                "headless": config.SCRAPLING_HEADLESS,
                "solve_cloudflare": config.SCRAPLING_SOLVE_CLOUDFLARE,
                "block_webrtc": config.SCRAPLING_BLOCK_WEBRTC,
                "hide_canvas": config.SCRAPLING_HIDE_CANVAS,
                "google_search": "startpage" not in request.url,
                "timeout": int(request.timeout * 1000),
            }
            
            # Neutral referer for Startpage
            if "startpage" in request.url:
                fetch_kwargs["extra_headers"] = {"Referer": "https://www.startpage.com/"}

            # Add proxy if available
            if request.proxy_url:
                fetch_kwargs["proxy"] = request.proxy_url

            # Add safe extra headers
            if request.headers:
                safe_headers = {}
                restricted = {"host", "connection", "accept", "accept-encoding", "user-agent", "content-length"}
                for k, v in request.headers.items():
                    if k.lower() not in restricted:
                        safe_headers[k] = v
                
                # Startpage-specific optimizations
                if "startpage" in request.url:
                    safe_headers["Accept-Language"] = "en-US,en;q=0.9"
                    safe_headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
                    safe_headers["Sec-Fetch-Dest"] = "document"
                    safe_headers["Sec-Fetch-Mode"] = "navigate"
                    safe_headers["Sec-Fetch-Site"] = "none"
                    safe_headers["Sec-Fetch-User"] = "?1"
                
                if safe_headers:
                    # Merge with existing extra_headers if any
                    if "extra_headers" in fetch_kwargs:
                        fetch_kwargs["extra_headers"].update(safe_headers)
                    else:
                        fetch_kwargs["extra_headers"] = safe_headers

            # ── Google-specific handling ──
            is_google_search = "google." in request.url and "/search" in request.url

            if is_google_search:
                # Try StealthyFetcher first with legacy-like headers (non-JS)
                fetch_kwargs["extra_headers"] = fetch_kwargs.get("extra_headers", {})
                fetch_kwargs["extra_headers"]["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0; rv:11.0) like Gecko"
                )

            if DynamicFetcher and is_google_search:
                fetcher_class = DynamicFetcher
                fetch_kwargs["wait_until"] = "networkidle"
                if request.timeout > 10:
                    fetch_kwargs["wait_for"] = "h3"
                    fetch_kwargs["wait_timeout"] = min(int(request.timeout * 1000 * 0.8), 30000)
            else:
                fetcher_class = StealthyFetcher

            # Use async_fetch for non-blocking operation
            response = await fetcher_class.async_fetch(**fetch_kwargs)

            if response is None:
                return self._make_result(
                    request, start_time,
                    success=False,
                    status_code=0,
                    error="Scrapling returned None (page failed to load)",
                )

            status_code = (
                response.status if hasattr(response, 'status') else
                response.status_code if hasattr(response, 'status_code') else
                200
            )
            
            # Scrapling Response usually holds content in .body or .content or .html_content
            if hasattr(response, 'body') and response.body:
                if isinstance(response.body, bytes):
                    html = response.body.decode('utf-8', errors='ignore')
                else:
                    html = str(response.body)
            elif hasattr(response, 'content') and response.content:
                if isinstance(response.content, bytes):
                    html = response.content.decode('utf-8', errors='ignore')
                else:
                    html = str(response.content)
            elif hasattr(response, 'html_content') and response.html_content:
                html = str(response.html_content)
            elif hasattr(response, 'text') and response.text:
                html = str(response.text)
            else:
                html = str(response) if response else ""
                
            success = status_code < 400 and len(html) > 100
            
            # Try to get cookies from response
            domain = urlparse(request.url).netloc
            try:
                if hasattr(response, 'cookies') and response.cookies:
                    cookie_dict = {}
                    for cookie in response.cookies:
                        if hasattr(cookie, 'name') and hasattr(cookie, 'value'):
                            cookie_dict[cookie.name] = cookie.value
                    if cookie_dict:
                        await self._cookie_manager.set_cookies(domain, cookie_dict)
            except Exception:
                pass  # Cookie extraction is best-effort

            # ── DOM cleaning for Google search results ──
            cleaned = None  # Initialize for Google result extraction
            if is_google_search and html:
                cleaned = clean_dom(html, url=request.url)
                if cleaned.success and cleaned.google_results:
                    html = cleaned.clean_html
                    logger.info(
                        f"ScraplingStrategy: Google DOM cleaned — "
                        f"{len(cleaned.google_results)} results extracted"
                    )

            result = self._make_result(
                request, start_time,
                success=status_code < 400 and len(html) > 100,
                status_code=status_code,
                final_url=request.url,
                html=html,
            )

            # Store Google results metadata for decision engine
            if is_google_search:
                has_h3 = "<h3" in html.lower() or "</h3>" in html.lower()
                result.metadata["google_has_h3"] = has_h3
                if cleaned and cleaned.success and cleaned.google_results:
                    result.metadata["google_results"] = cleaned.google_results
                    result.metadata["google_results_markdown"] = make_google_results_markdown(cleaned.google_results)

            if result.is_blocked:
                result.success = False
                logger.debug(f"ScraplingStrategy: blocked on {request.url}")

            return result

        except Exception as e:
            logger.warning(f"ScraplingStrategy error on {request.url}: {e}")
            return self._make_result(
                request, start_time,
                success=False,
                status_code=0,
                error=str(e),
            )

    async def shutdown(self) -> None:
        """Scrapling handles browser cleanup internally."""
        pass
