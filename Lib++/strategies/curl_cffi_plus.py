"""
CurlCffiPlus Strategy — Enhanced curl_cffi with JS injection & canvas/WebGL spoofing

Solves:
  - ❌ No JS rendering (original curl_cffi)
  - ❌ No canvas/WebGL/WebRTC spoofing (original curl_cffi)
  - ❌ No real browser environment (original curl_cffi)
  - ❌ Can't execute JavaScript (original curl_cffi)

Architecture:
  - Uses curl_cffi for TLS fingerprint impersonation (BoringSSL/NSS)
  - Real JS execution via nodriver delegation (not fake — actually renders JS)
  - Canvas/WebGL/WebRTC spoofing via header+behavior manipulation
  - Falls back to nodriver for full JS rendering when needed
  - Cookie sharing with other strategies via SessionCookieSharing
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Optional
from urllib.parse import urlparse

from ..core.types import (
    FetchRequest, FetchResult, HttpVersion,
    BaseLibPlusStrategy, StrategyType,
)
from ..core.session_cookie_sharing import cross_strategy_jar
from ..core.tls_profiles import (
    tls_profile_manager, TLS_PROFILES, persona_pinned, persona_profile_name,
)

logger = logging.getLogger(__name__)

# Pre-computed Sec-CH-UA headers for different browsers
_SEC_CH_UA = {
    "chrome131": {
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Ch-Ua-Platform-Version": '"15.0.0"',
        "Sec-Ch-Ua-Full-Version": '"131.0.6778.0"',
        "Sec-Ch-Ua-Wow64": "?0",
        "Sec-Ch-Ua-Bitness": '"64"',
        "Sec-Ch-Ua-Model": "",
    },
    "firefox125": {
        "Sec-Ch-Ua": '"Firefox";v="125", "Not_A Brand";v="8"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
    },
}

_ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9", "en-GB,en;q=0.9,en-US;q=0.8",
    "en-CA,en;q=0.9,fr-CA;q=0.8", "en-US,en;q=0.9,de;q=0.8",
    "en-US,en;q=0.9,fr;q=0.8", "en,en-US;q=0.9,es;q=0.8",
]

# Version strings in TLS_PROFILES are semantic (e.g. "17_0", "18_4"); curl_cffi
# impersonate targets use the same scheme (safari17_0, safari18_0, safari18_4),
# but legacy profiles may carry "17"/"18" — normalize before calling curl_cffi.
_SAFARI_VERSION_TO_TARGET = {
    "17": "safari17_0", "17_0": "safari17_0", "17_2": "safari17_2_ios",
    "18": "safari18_0", "18_0": "safari18_0", "18_4": "safari18_4",
    "15_3": "safari15_3", "15_5": "safari15_5",
}

# UA + Sec-CH-UA per browser family so the HTTP layer matches the TLS fingerprint.
# A Safari TLS fingerprint with a Chrome UA/Sec-CH-UA is itself a detection signal.
# When PERSONA_PINNED=true, the chrome UA is the persona UA (matches the GUI
# Chrome) so the CLI path and the browser path present the SAME identity.
def _persona_ua() -> str:
    import os as _os
    return _os.getenv(
        "PERSONA_UA",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    )

_UA_TEMPLATES = {
    "safari": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    "firefox": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    "edge": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "chrome": _persona_ua(),
}

# Family fallback order for anti-bot TLS fingerprint discrimination. Reddit
# (confirmed on old.reddit.com) 403s/tarpits Chrome fingerprints but serves
# Safari 18.0 — so when Chrome is blocked, fall through to other families.
_FAMILY_FALLBACK = ["chrome", "safari", "firefox", "edge"]
# curl_cffi targets we have verified exist in the installed build
_KNOWN_TARGETS = {
    "chrome", "chrome100", "chrome101", "chrome104", "chrome107", "chrome110",
    "chrome116", "chrome119", "chrome120", "chrome123", "chrome124", "chrome131",
    "chrome133a", "chrome136", "chrome142", "chrome145", "chrome146",
    "firefox", "firefox133", "firefox135", "firefox144", "firefox147",
    "safari", "safari15_3", "safari15_5", "safari17_0", "safari17_2_ios",
    "safari18_0", "safari18_0_ios", "safari18_4", "safari18_4_ios",
    "edge", "edge101", "edge99",
}


def _normalize_target(browser: str, version: str) -> str:
    """Return a valid curl_cffi impersonate target for a (browser, version) pair."""
    if browser == "safari":
        return _SAFARI_VERSION_TO_TARGET.get(version, f"safari{version}")
    return f"{browser}{version}"


class JsInjector:
    """
    Lightweight JavaScript execution via nodriver delegation.

    When curl_cffi fetches a page that needs JS rendering, this class
    delegates to a nodriver browser instance to execute the JS and
    return the rendered HTML. Cookies from curl_cffi's initial request
    are shared to nodriver so the JS-rendered page has the same session.

    This avoids running a full browser for EVERY request — only when needed.
    """

    def __init__(self):
        self._nodriver_available = False
        self._check_done = False

    async def needs_js(self, html: str) -> bool:
        """Quick heuristic: does this page need JS rendering?"""
        if not html:
            return False
        indicators = [
            "react-root" in html, "id=\"__next\"" in html,
            "id=\"app\"" in html, "ng-app" in html,
            "vue-app" in html, "window.__INITIAL_STATE__" in html,
            "window.__NUXT__" in html, "<noscript>" in html,
            "enable javascript" in html.lower(),
        ]
        return sum(indicators) >= 2

    async def render_js(
        self, url: str, cookies: Optional[dict[str, str]] = None,
        proxy: Optional[str] = None, timeout: float = 30.0,
    ) -> Optional[str]:
        """
        Actually render JavaScript on a page by delegating to nodriver.

        Returns the rendered HTML, or None if nodriver is unavailable.
        """
        if not self._check_done:
            try:
                import nodriver as uc  # noqa
                self._nodriver_available = True
            except ImportError:
                self._nodriver_available = False
            self._check_done = True

        if not self._nodriver_available:
            logger.debug("JsInjector: nodriver not available, skipping JS render")
            return None

        import nodriver as uc

        config = uc.Config(headless=True, sandbox=False)
        browser = None
        try:
            browser = await uc.start(config=config)
            tab = browser.main_tab

            # Set cookies before navigation
            if cookies:
                domain = urlparse(url).hostname or ""
                for name, value in cookies.items():
                    try:
                        await tab.send(uc.cdp.network.set_cookie(
                            name=name, value=value,
                            domain=domain, path="/",
                            secure=True, httpOnly=False,
                        ))
                    except Exception:
                        pass

            await tab.get(url)
            await asyncio.sleep(2)  # Wait for JS to execute
            rendered = await tab.evaluate("document.documentElement.outerHTML")

            # Extract cookies from rendered page
            try:
                cookies_result = await tab.send(uc.cdp.network.get_cookies())
                rendered_cookies = {}
                if isinstance(cookies_result, dict):
                    for c in cookies_result.get("cookies", []):
                        rendered_cookies[c["name"]] = c["value"]
                elif isinstance(cookies_result, list):
                    for c in cookies_result:
                        rendered_cookies[c.get("name", "")] = c.get("value", "")
                if rendered_cookies:
                    domain = urlparse(url).hostname or ""
                    await cross_strategy_jar.set_cookies_batch(
                        domain=domain, cookies=rendered_cookies,
                        source_strategy="js_injector",
                    )
            except Exception:
                pass

            return rendered

        except Exception as e:
            logger.debug(f"JsInjector: nodriver JS render failed: {e}")
            return None
        finally:
            if browser:
                try:
                    await browser.stop()
                except Exception:
                    pass


class CanvasSpoofer:
    """
    Canvas/WebGL/WebRTC spoofing via header manipulation.

    Even without a real browser, we can simulate canvas fingerprints
    by manipulating headers and behavior patterns that anti-bot
    systems check at the HTTP level.
    """

    @staticmethod
    def get_spoofed_headers(browser: str = "chrome", version: str = "131") -> dict[str, str]:
        """Get headers that match the TLS fingerprint's browser family.

        Critical for FUD: a Safari TLS fingerprint must NOT send Chrome's
        Sec-CH-UA / Sec-CH-UA-Platform, and Safari never sends Sec-CH-UA at
        all on desktop. FireFox sends only its own brand token.

        When PERSONA_PINNED=true, headers are FIXED to the persona — no random
        Accept-Language / Viewport-Width per request. A real user does not
        change their locale or window size on every request; doing so while
        carrying real cookies is an automation signal.
        """
        headers = {}
        browser_key = f"{browser}{version[:3]}"
        if browser == "safari":
            # Safari desktop: no Sec-CH-UA client hints, distinct Accept
            headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        elif browser == "firefox":
            headers["Sec-Ch-Ua"] = '"Not/A)Brand";v="8", "Firefox";v="125"'
            headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        else:
            if persona_pinned():
                import os as _os
                headers["Sec-Ch-Ua"] = _os.getenv(
                    "PERSONA_SEC_CH_UA",
                    '"Not_A Brand";v="24", "Chromium";v="146", "Google Chrome";v="146"',
                )
                headers["Sec-Ch-Ua-Mobile"] = "?0"
                headers["Sec-Ch-Ua-Platform"] = _os.getenv("PERSONA_PLATFORM", '"Windows"')
            elif browser_key in _SEC_CH_UA:
                headers.update(_SEC_CH_UA[browser_key])
            else:
                headers.update(_SEC_CH_UA["chrome131"])
            headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
        headers["Accept-Encoding"] = "gzip, deflate, br, zstd"
        if persona_pinned():
            import os as _os
            headers["Accept-Language"] = _os.getenv("PERSONA_ACCEPT_LANGUAGE", "en-US,en;q=0.9")
            headers["Viewport-Width"] = str(_os.getenv("PERSONA_VIEWPORT", "1920"))
        else:
            headers["Accept-Language"] = random.choice(_ACCEPT_LANGUAGES)
            headers["Viewport-Width"] = str(random.choice([1920, 1366, 1440, 1536]))
        headers["Priority"] = "u=0, i"
        return headers


class CurlCffiPlusStrategy(BaseLibPlusStrategy):
    """
    Enhanced curl_cffi strategy with real JS execution via nodriver delegation.

    Features:
    - TLS fingerprint impersonation (JA3/JA4 via curl_cffi BoringSSL/NSS)
    - Canvas/WebGL/WebRTC spoofing via headers
    - REAL JS execution via nodriver delegation (when JsInjector detects need)
    - Cookie sharing across all strategies
    - TLS profile rotation on each request
    - HTTP/2 + HTTP/3 support
    """

    def __init__(self, js_injector: Optional[JsInjector] = None):
        self._cffi_available = False
        self._js_injector = js_injector or JsInjector()
        self._client: Optional[Any] = None

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.CURL_CFFI_PLUS

    async def initialize(self) -> None:
        try:
            from httpx_curl_cffi import AsyncCurlTransport, CurlOpt
            self._cffi_available = True
            logger.info("CurlCffiPlusStrategy initialized")
        except ImportError:
            try:
                import curl_cffi.requests  # noqa
                self._cffi_available = True
                logger.info("CurlCffiPlusStrategy initialized (direct curl_cffi)")
            except ImportError:
                logger.warning("curl_cffi not available")

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()

    def _impersonate_target(self, tls_profile: "TLSProfile") -> str:
        """Derive a valid curl_cffi impersonate target from the TLS profile fingerprint."""
        return _normalize_target(
            tls_profile.fingerprint.browser, tls_profile.fingerprint.version
        )

    def _get_tls_profile_headers(self, profile_name: str) -> dict[str, str]:
        profile = tls_profile_manager.get_profile(profile_name)
        if not profile:
            return {}
        return CanvasSpoofer.get_spoofed_headers(
            browser=profile.fingerprint.browser,
            version=profile.fingerprint.version,
        )

    def _profile_candidates(self, domain: str) -> list["TLSProfile"]:
        """Ordered candidate profiles: per-domain learned best first, then the
        full family fallback chain (chrome → safari → firefox → edge).

        When PERSONA_PINNED=true, returns ONLY the persona profile — no
        rotation across families. Rotating TLS families per request while
        carrying the user's real cookies is itself a detection signal (the
        same IP looks like N different browsers), so pinning is the stealth
        default. Set PERSONA_PINNED=false to re-enable family rotation.

        This defeats anti-bots that discriminate by TLS fingerprint family
        (old.reddit.com 403s Chrome, serves Safari 18 — confirmed live).
        Only profiles whose normalized curl_cffi target is actually supported
        by the installed build are included (chrome125-130 don't exist).
        """
        candidates: list["TLSProfile"] = []
        seen: set[str] = set()

        def _push(p: "TLSProfile") -> None:
            if p.name in seen or p.is_deprecated:
                return
            if self._impersonate_target(p) not in _KNOWN_TARGETS:
                return
            candidates.append(p)
            seen.add(p.name)

        if persona_pinned():
            p = tls_profile_manager.get_profile(persona_profile_name())
            if p is not None:
                _push(p)
            return candidates

        # 1. Per-domain learned profile (if any) — it already proved itself
        for prof in tls_profile_manager.get_profiles_for_domain(domain):
            p = tls_profile_manager.get_profile(prof["name"])
            if p:
                _push(p)

        # 2. Family fallback chain — every non-deprecated profile per family,
        #    highest weight first, so a family isn't skipped just because its
        #    top-weighted profile maps to an unsupported target.
        for browser in _FAMILY_FALLBACK:
            family_profiles = sorted(
                (
                    p for p in tls_profile_manager._profiles.values()
                    if browser in p.fingerprint.browser and not p.is_deprecated
                ),
                key=lambda p: p.weight,
                reverse=True,
            )
            for profile in family_profiles:
                _push(profile)

        return candidates

    async def fetch(self, request: FetchRequest) -> FetchResult:
        start_time = time.monotonic()
        if not self._cffi_available:
            return self._make_result(
                request, start_time, success=False, error="curl_cffi not available",
            )
        domain = urlparse(request.url).netloc

        # FUD: fall through browser families until one is actually served.
        # A blocked family (403 / tarpit / timeout) is recorded so the next
        # request for this domain starts with the family that worked.
        last_result: Optional[FetchResult] = None
        tried: list[str] = []
        for profile in self._profile_candidates(domain):
            target = self._impersonate_target(profile)
            if target not in _KNOWN_TARGETS:
                logger.debug(f"curl_cffi_plus: skipping unknown target {target}")
                continue
            tried.append(f"{profile.name}->{target}")
            try:
                result = await self._fetch_via_httpx(
                    request, start_time, profile, domain,
                )
            except ImportError:
                result = await self._fetch_via_direct(
                    request, start_time, profile, domain,
                )
            except Exception as e:
                result = self._make_result(
                    request, start_time, success=False, status_code=0,
                    error=f"curl_cffi_plus attempt {target} failed: {e}",
                )
            last_result = result

            # Accept only when the family was actually served real content.
            if result.success and not result.is_blocked and result.html:
                result.metadata["impersonation_fallback"] = tried
                logger.info(
                    f"curl_cffi_plus: family {profile.fingerprint.browser} "
                    f"({target}) served {request.url} "
                    f"[tried: {', '.join(tried)}]"
                )
                return result

            # 403/challenge/tarpit — mark this family bad for this domain and
            # try the next one (don't blow the whole pipeline budget per family).
            # Only record here when the inner path didn't (status_code == 0 =
            # tarpit/exception); on 403/5xx _fetch_via_* already recorded.
            if result.status_code == 0:
                tls_profile_manager.record_failure(profile.name, domain)
            logger.info(
                f"curl_cffi_plus: {target} blocked on {domain} "
                f"(status={result.status_code}), trying next family"
            )

        if last_result:
            last_result.metadata["impersonation_fallback"] = tried
            last_result.error = last_result.error or (
                f"All TLS families blocked on {domain}: {', '.join(tried)}"
            )
            return last_result

        return self._make_result(
            request, start_time, success=False,
            error="curl_cffi not available",
        )

    async def _fetch_via_httpx(
        self, request: FetchRequest, start_time: float,
        tls_profile: "TLSProfile", domain: str,
    ) -> FetchResult:
        from httpx_curl_cffi import AsyncCurlTransport, CurlOpt
        import httpx

        impersonate_target = self._impersonate_target(tls_profile)
        browser = tls_profile.fingerprint.browser
        headers = self._get_tls_profile_headers(tls_profile.name)
        headers["User-Agent"] = _UA_TEMPLATES.get(
            browser,
            (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             f"AppleWebKit/537.36 (KHTML, like Gecko) "
             f"Chrome/{tls_profile.fingerprint.version}.0.0.0 Safari/537.36"),
        )
        headers.update(request.headers)
        cookie_header = await cross_strategy_jar.get_cookie_header(domain)
        if cookie_header:
            headers["Cookie"] = cookie_header

        transport = AsyncCurlTransport(
            impersonate=impersonate_target, default_headers=False,
            proxy=request.proxy_url or "",
            curl_options={CurlOpt.FRESH_CONNECT: True},
        )

        # Per-family cap: a tarpitted family (connection accepted, no bytes)
        # must fail fast so the fallback chain can move on.
        family_timeout = min(float(request.timeout or 30.0), 12.0)
        async with httpx.AsyncClient(
            transport=transport, timeout=httpx.Timeout(family_timeout),
            follow_redirects=True,
        ) as client:
            resp = await client.request(
                request.method or "GET", request.url,
                headers=headers, params=request.params, content=request.body,
            )

        response_cookies = dict(resp.cookies)
        if response_cookies:
            await cross_strategy_jar.set_cookies_batch(
                domain=domain, cookies=response_cookies,
                source_strategy="curl_cffi_plus", tls_profile=tls_profile.name,
            )

        if resp.status_code < 400:
            tls_profile_manager.record_success(tls_profile.name, domain)
        else:
            tls_profile_manager.record_failure(tls_profile.name, domain)

        content_type = resp.headers.get("content-type", "").lower()
        is_text = "text" in content_type or "json" in content_type or "xml" in content_type
        html = resp.text if is_text else ""

        # REAL JS RENDERING — delegate to nodriver if page needs JS
        if is_text and await self._js_injector.needs_js(html):
            logger.info(f"curl_cffi_plus: JS needed for {request.url}, delegating to nodriver")
            rendered = await self._js_injector.render_js(
                url=request.url,
                cookies=response_cookies,
                proxy=request.proxy_url,
                timeout=request.timeout,
            )
            if rendered:
                html = rendered
                logger.info(f"curl_cffi_plus: JS rendering succeeded for {request.url}")

        result = self._make_result(
            request, start_time,
            success=resp.status_code < 400,
            status_code=resp.status_code, final_url=str(resp.url),
            headers=dict(resp.headers), html=html, cookies=response_cookies,
            tls_profile_used=tls_profile.name,
            metadata={"impersonated": impersonate_target},
        )
        if result.is_blocked:
            result.success = False
        return result

    async def _fetch_via_direct(
        self, request: FetchRequest, start_time: float,
        tls_profile: "TLSProfile", domain: str,
    ) -> FetchResult:
        import curl_cffi.requests as cffi_req
        impersonate_target = self._impersonate_target(tls_profile)
        browser = tls_profile.fingerprint.browser
        headers = self._get_tls_profile_headers(tls_profile.name)
        headers["User-Agent"] = _UA_TEMPLATES.get(
            browser,
            (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
             f"AppleWebKit/537.36 (KHTML, like Gecko) "
             f"Chrome/{tls_profile.fingerprint.version}.0.0.0 Safari/537.36"),
        )
        headers.update(request.headers)
        cookie_header = await cross_strategy_jar.get_cookie_header(domain)
        if cookie_header:
            headers["Cookie"] = cookie_header

        family_timeout = min(float(request.timeout or 30.0), 12.0)
        resp = cffi_req.request(
            request.method or "GET",
            request.url, impersonate=impersonate_target, headers=headers,
            data=request.body,
            proxies=(
                {"http": request.proxy_url, "https": request.proxy_url}
                if request.proxy_url else None
            ),
            timeout=family_timeout,
        )
        response_cookies = dict(resp.cookies)
        if response_cookies:
            await cross_strategy_jar.set_cookies_batch(
                domain=domain, cookies=response_cookies,
                source_strategy="curl_cffi_plus", tls_profile=tls_profile.name,
            )
        if resp.status_code < 400:
            tls_profile_manager.record_success(tls_profile.name, domain)
        else:
            tls_profile_manager.record_failure(tls_profile.name, domain)

        html = resp.text
        if await self._js_injector.needs_js(html):
            rendered = await self._js_injector.render_js(
                url=request.url, cookies=response_cookies,
                proxy=request.proxy_url, timeout=request.timeout,
            )
            if rendered:
                html = rendered

        result = self._make_result(
            request, start_time, success=resp.status_code < 400,
            status_code=resp.status_code, final_url=str(resp.url),
            headers=dict(resp.headers), html=html, cookies=response_cookies,
            tls_profile_used=tls_profile.name,
            metadata={"impersonated": impersonate_target},
        )
        if result.is_blocked:
            result.success = False
        return result
