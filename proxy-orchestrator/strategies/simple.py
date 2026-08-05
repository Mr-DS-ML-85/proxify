"""
Simple Strategy — Fast HTTP client with realistic browser headers.
Uses httpx with HTTP/2, TLS fingerprint impersonation via curl_cffi,
full header suite from solve-403 patterns.
"""

import asyncio
import logging
import random
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from config import config
from strategies.base import BaseStrategy, FetchRequest, FetchResult
from services.ua_pool import ua_pool
from services.cookie_jar import CookieManager
from engine.dom_cleaner import clean_dom, make_google_results_markdown

logger = logging.getLogger(__name__)

# Realistic header templates based on solve-403 best practices
_ACCEPT_HEADERS = {
    "text/html": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "json": "application/json, text/plain, */*",
    "any": "*/*",
}

_ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.9,de;q=0.8",
    "en-US,en;q=0.9,fr;q=0.8",
    "en-US,en;q=0.9,es;q=0.8",
    "en,en-US;q=0.9",
]

# STEALTH PERSONA — when PERSONA_PINNED=true the pipeline uses ONLY the
# persona TLS target + persona UA + persona headers. No rotation. This makes
# the CLI/HTTP path present the EXACT same identity as the GUI Chrome, which
# is what Google's risk engine expects from a real user carrying real cookies.
if config.PERSONA_PINNED:
    _IMPRESONATE_TARGETS = [config.PERSONA_TLS]
else:
    _IMPRESONATE_TARGETS = [
        t.strip() for t in config.CURL_CFFI_IMPERSONATE.split(",") if t.strip()
    ]
if not _IMPRESONATE_TARGETS:
    _IMPRESONATE_TARGETS = ["chrome146"]


class SimpleStrategy(BaseStrategy):
    """
    Fast HTTP strategy using httpx + curl_cffi TLS fingerprint impersonation.
    Implements techniques from solve-403-in-web-scraping:
    - TLS/JA3 impersonation via curl_cffi (BoringSSL/NSS instead of OpenSSL)
    - Full browser-like headers (Accept, Accept-Language, Accept-Encoding, DNT, Referer)
    - HTTP/2 support
    - Connection pooling
    - Cookie persistence via CookieManager
    - TLS fingerprint rotation on proxy-switched connections
    """

    def __init__(self, cookie_manager: CookieManager) -> None:
        self._cookie_manager = cookie_manager
        self._client: Optional[httpx.AsyncClient] = None
        self._curl_cffi_enabled = config.CURL_CFFI_ENABLED
        self._impersonate_index = 0
        self._current_impersonate_target: str = ""

    @property
    def current_impersonate_target(self) -> str:
        """The TLS fingerprint family the shared client currently impersonates."""
        return self._current_impersonate_target or (_IMPRESONATE_TARGETS[0] if _IMPRESONATE_TARGETS else "")

    @property
    def name(self) -> str:
        return "simple"

    @property
    def priority(self) -> int:
        return 10  # Highest priority (tried first)

    def _next_impersonate_target(self) -> str:
        """Rotate through impersonation targets for TLS fingerprint diversity."""
        target = _IMPRESONATE_TARGETS[self._impersonate_index % len(_IMPRESONATE_TARGETS)]
        self._impersonate_index += 1
        return target

    def _build_curl_cffi_transport(
        self, proxy: Optional[str] = None
    ) -> Optional["httpx.BaseTransport"]:
        """Build an AsyncCurlTransport with TLS fingerprint impersonation.
        Returns None if curl_cffi is disabled or unavailable."""
        if not self._curl_cffi_enabled:
            return None
        try:
            from httpx_curl_cffi import AsyncCurlTransport, CurlOpt

            target = self._next_impersonate_target()
            self._current_impersonate_target = target
            logger.debug(f"curl_cffi: impersonating {target}")
            return AsyncCurlTransport(
                impersonate=target,
                default_headers=False,  # Our _build_headers() handles headers
                proxy=proxy,
                curl_options={CurlOpt.FRESH_CONNECT: True},
            )
        except ImportError:
            self._curl_cffi_enabled = False
            logger.warning(
                "httpx-curl-cffi not installed — falling back to native httpx. "
                "Install with: pip install httpx-curl-cffi"
            )
            return None

    def _create_client(
        self,
        transport: Optional["httpx.BaseTransport"] = None,
        proxy: Optional[str] = None,
        timeout: float = 30.0,
    ) -> httpx.AsyncClient:
        """Create an httpx AsyncClient, preferring curl_cffi transport."""
        common = dict(
            follow_redirects=True,
            verify=True,
        )
        if transport:
            return httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(timeout, connect=timeout),
                **common,
            )
        # Native httpx fallback
        return httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(timeout, connect=timeout),
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30.0,
            ),
            **({} if not proxy else {"proxy": proxy}),
        )

    async def initialize(self) -> None:
        """Create the persistent httpx client with curl_cffi TLS impersonation."""
        transport = self._build_curl_cffi_transport()
        self._client = self._create_client(transport=transport)
        engine = "curl_cffi" if self._curl_cffi_enabled else "native httpx"
        logger.info(f"SimpleStrategy initialized with {engine} (impersonating: {_IMPRESONATE_TARGETS})")

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()

    def _build_headers(self, request: FetchRequest, user_agent: str) -> dict[str, str]:
        """Build realistic browser headers following solve-403 patterns.

        The header suite is matched to the TLS fingerprint family the shared
        client is impersonating (Chrome vs Safari vs Firefox). Mismatched
        Sec-CH-UA / UA vs TLS fingerprint is itself an automation signal.

        When PERSONA_PINNED=true the Accept-Language is FIXED to the persona
        value (a real user doesn't change their locale per request) and the
        Sec-CH-UA hints match the persona version.
        """
        parsed = urlparse(request.url)
        is_legacy = "Trident" in user_agent or "MSIE" in user_agent
        family = self.current_impersonate_target

        headers = {
            "User-Agent": user_agent,
            "Accept": _ACCEPT_HEADERS["text/html"],
            "Accept-Language": (
                config.PERSONA_ACCEPT_LANGUAGE
                if config.PERSONA_PINNED
                else random.choice(_ACCEPT_LANGUAGES)
            ),
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }

        if is_legacy:
            headers["DNT"] = "1"
        elif family.startswith("safari"):
            # Safari desktop sends NO Sec-CH-UA client hints
            headers.update({
                "DNT": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Cache-Control": "max-age=0",
            })
        elif family.startswith("firefox"):
            headers.update({
                "DNT": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Firefox";v="125"',
                "Cache-Control": "max-age=0",
            })
        else:
            headers.update({
                "DNT": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "Sec-Ch-Ua": (
                    config.PERSONA_SEC_CH_UA
                    if config.PERSONA_PINNED
                    else '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'
                ),
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": (
                    config.PERSONA_PLATFORM
                    if config.PERSONA_PINNED
                    else '"Windows"'
                ),
                "Cache-Control": "max-age=0",
            })

        # Add Referer (except for direct Google search)
        if "Referer" not in request.headers and "google." not in request.url:
            headers["Referer"] = "https://www.google.com/"

        # Merge user-specified headers (they take priority)
        headers.update(request.headers)
        return headers

    async def _try_fetch_with_ua(
        self,
        request: FetchRequest,
        user_agent: str,
        proxy: Optional[str],
        start_time: float,
    ) -> FetchResult:
        """Try a fetch with a specific User-Agent. Used for fallback attempts."""
        # Match UA to the TLS fingerprint family being impersonated — a Safari
        # TLS fingerprint with a Chrome UA is an automation signal.
        family = self.current_impersonate_target
        if family.startswith("safari"):
            user_agent = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/18.0 Safari/605.1.15"
            )
        elif family.startswith("firefox"):
            user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
                "Gecko/20100101 Firefox/125.0"
            )
        elif family.startswith("edge"):
            user_agent = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
            )
        headers = self._build_headers(request, user_agent)
        domain = urlparse(request.url).netloc

        cookie_header = await self._cookie_manager.export_as_header(domain)
        if cookie_header:
            existing = headers.get("Cookie", "")
            headers["Cookie"] = f"{existing}; {cookie_header}" if existing else cookie_header

        if proxy:
            transport = self._build_curl_cffi_transport(proxy=proxy)
            client = self._create_client(transport=transport, proxy=proxy, timeout=request.timeout)
        else:
            client = self._client

        try:
            if request.method.upper() == "GET":
                response = await client.get(
                    request.url,
                    headers=headers,
                    params=request.params,
                )
            elif request.method.upper() == "POST":
                response = await client.post(
                    request.url,
                    headers=headers,
                    content=request.body,
                    params=request.params,
                )
            else:
                response = await client.request(
                    request.method,
                    request.url,
                    headers=headers,
                    content=request.body,
                    params=request.params,
                )
        finally:
            if proxy and client is not self._client:
                await client.aclose()

        response_cookies = dict(response.cookies)
        if response_cookies:
            await self._cookie_manager.set_cookies(domain, response_cookies)

        content_type = response.headers.get("content-type", "").lower()
        is_text = "text" in content_type or "json" in content_type or "xml" in content_type

        result = self._make_result(
            request,
            start_time,
            success=200 <= response.status_code < 400,
            status_code=response.status_code,
            final_url=str(response.url),
            headers=dict(response.headers),
            html=response.text if is_text else "",
            body=response.content,
            cookies=response_cookies,
        )

        if result.is_blocked:
            result.success = False
        return result

    async def _reset_client(self) -> None:
        """Close the shared client and drop it so the next fetch re-initializes.

        Fixes stale keepalive connections: the shared curl_cffi transport reuses
        a pooled connection that the server has silently closed, and the request
        hangs until timeout even though the network is fine (raw curl is ~0.3s).
        """
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def fetch(self, request: FetchRequest) -> FetchResult:
        """Execute HTTP request with TLS fingerprint impersonation + realistic headers.

        For Google search URLs, uses a two-phase approach:
          1. First try with a legacy (IE11) UA to get non-JS Google HTML
             (simpler, cleaner, no JS rendering needed)
          2. If that fails, try with modern Chrome UA

        Wrapped in a hard per-request cap (default 20s): this is the FIRST,
        fast strategy — if it can't get content quickly it must fail fast so the
        browser strategies get their fair share of the pipeline budget instead
        of being starved by a hung HTTP request.
        """
        start_time = time.monotonic()
        hard_cap = min(float(request.timeout or 30), 20.0)
        try:
            return await asyncio.wait_for(
                self._fetch_impl(request, start_time),
                timeout=hard_cap,
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"SimpleStrategy timeout after {hard_cap:.0f}s ({request.url[:60]}) "
                f"— resetting shared client (stale pool guard)"
            )
            await self._reset_client()
            return self._make_result(
                request, start_time,
                success=False, status_code=0,
                error=f"SimpleStrategy timeout after {hard_cap:.0f}s",
            )

    async def _fetch_impl(self, request: FetchRequest, start_time: float) -> FetchResult:
        """The actual fetch logic (wrapped by fetch()'s hard cap)."""
        if not self._client:
            await self.initialize()

        try:
            session_id = request.session_id or urlparse(request.url).netloc
            domain = urlparse(request.url).netloc
            proxy = request.proxy_url

            # ── Google-specific: try persona UA first, legacy only as fallback ──
            is_google_search = (
                "google." in request.url
                and "/search" in request.url
            )

            if is_google_search and config.PERSONA_PINNED:
                # When the persona is pinned, ALWAYS present the persona UA —
                # the legacy IE11 UA trick creates a SECOND identity from the
                # same IP (Google sees IE11 + Chrome/146 + your cookies), which
                # is exactly the inconsistency that triggers the risk engine.
                # NOTE: this is the ONLY fetch for Google when pinned — the
                # result below (even a stub) is returned as-is; we must NOT
                # fall through to a second identical persona-UA fetch.
                user_agent = config.PERSONA_UA
                logger.debug(f"SimpleStrategy: Google via persona UA ({user_agent[:40]}...)")
                result = await self._try_fetch_with_ua(
                    request, user_agent, proxy, start_time,
                )
                if result.success and not result.is_blocked and result.html:
                    has_real_content = (
                        "<h3" in result.html
                        or '<div class="g"' in result.html
                        or '<div id="search"' in result.html
                    )
                    if has_real_content and len(result.html) > 5000:
                        cleaned = clean_dom(result.html, url=request.url)
                        if cleaned.success and cleaned.google_results:
                            result.html = cleaned.clean_html or ""
                            result.metadata["google_results"] = cleaned.google_results
                            result.metadata["google_results_markdown"] = make_google_results_markdown(cleaned.google_results)
                            result.metadata["google_method"] = "persona_ua"
                            logger.info(
                                f"SimpleStrategy: Google success via persona UA "
                                f"({len(cleaned.google_results)} results)"
                            )
                            return result
                # Persona attempt done — return it (real content or stub) so we
                # never fire a second identical request at Google.
                return result

            if is_google_search and not config.PERSONA_PINNED:
                # Legacy IE11 UA trick (only when NOT pinned — it is a distinct
                # identity and therefore a consistency hazard with real cookies)
                legacy_ua = ua_pool.get_legacy_ua()
                logger.debug(f"SimpleStrategy: trying legacy UA for Google: {legacy_ua[:40]}...")
                result = await self._try_fetch_with_ua(
                    request, legacy_ua, proxy, start_time,
                )

                if result.success and not result.is_blocked and result.html:
                    has_real_content = (
                        "<h3" in result.html
                        or '<div class="g"' in result.html
                        or '<div id="search"' in result.html
                    )
                    if has_real_content and len(result.html) > 5000:
                        cleaned = clean_dom(result.html, url=request.url)
                        if cleaned.success and cleaned.google_results:
                            result.html = cleaned.clean_html or ""
                            result.metadata["google_results"] = cleaned.google_results
                            result.metadata["google_results_markdown"] = make_google_results_markdown(cleaned.google_results)
                            result.metadata["google_method"] = "legacy_ua"
                            logger.info(
                                f"SimpleStrategy: Google success via legacy UA "
                                f"({len(cleaned.google_results)} results)"
                            )
                            return result

                logger.debug("SimpleStrategy: legacy UA failed for Google, trying modern UA")

            # ── Normal flow: modern Chrome UA (persona when pinned) ──
            user_agent = (
                config.PERSONA_UA
                if config.PERSONA_PINNED
                else ua_pool.get_for_session(session_id)
            )
            logger.debug(f"SimpleStrategy: session_id={session_id} using UA={user_agent}")

            result = await self._try_fetch_with_ua(
                request, user_agent, proxy, start_time,
            )

            # Post-process Google results with DOM cleaner
            if is_google_search and result.success and result.html:
                cleaned = clean_dom(result.html, url=request.url)
                if cleaned.success and cleaned.google_results:
                    result.html = cleaned.clean_html or ""
                    result.metadata["google_results"] = cleaned.google_results
                    result.metadata["google_results_markdown"] = make_google_results_markdown(cleaned.google_results)
                    result.metadata["google_method"] = "modern_ua"

            return result

        except httpx.TimeoutException as e:
            logger.debug(f"SimpleStrategy timeout: {request.url}: {e}")
            return self._make_result(
                request, start_time,
                success=False,
                status_code=0,
                error=f"Timeout: {e}",
            )
        except Exception as e:
            logger.debug(f"SimpleStrategy error: {request.url}: {e}")
            return self._make_result(
                request, start_time,
                success=False,
                status_code=0,
                error=str(e),
            )
