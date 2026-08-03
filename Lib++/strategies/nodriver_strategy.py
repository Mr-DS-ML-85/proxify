"""
Nodriver Strategy — CDP-Direct Browser Automation for Lib++

Solves ALL browser-based strategy disadvantages:
  - ❌ navigator.webdriver detectable (Playwright/Puppeteer)
  - ❌ CDP leaks possible (Playwright/Puppeteer)
  - ❌ No native TLS fingerprint control (Playwright)
  - ❌ Heavy browser overhead → pooled instances
  - ❌ Subprocess overhead (Puppeteer)
  - ❌ Firefox-based different TLS (Scrapling)
  - ❌ Selenium-based slow (FlareSolverr)

Internally contains:
  - NodriverPool — spawn/manage nodriver instances with full anti-fingerprint
  - StealthBrowserClient — connect to EXISTING stealth-browser HTTP service
    (no MCP, uses REST directly, internal to this module)
  - AntiFingerprintLoader — loads and caches the anti-fingerprinting JS
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from ..core.types import (
    FetchRequest, FetchResult, BaseLibPlusStrategy, StrategyType,
)
from ..core.session_cookie_sharing import cross_strategy_jar

logger = logging.getLogger(__name__)

# =============================================================================
# Internal: Anti-Fingerprint Script Loader
# =============================================================================

class AntiFingerprintLoader:
    """Loads and caches the anti-fingerprinting script from disk."""
    _script: Optional[str] = None
    _fallback = r"""
(function() {
    'use strict';
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    if (!window.chrome) window.chrome = { runtime: { connect: () => ({}), sendMessage: () => {},
        onMessage: { addListener: () => {} }, onConnect: { addListener: () => {} } } };
    const q = navigator.permissions.query;
    navigator.permissions.query = (p) => p.name === 'notifications'
        ? Promise.resolve({ state: 'prompt' }) : q(p);
    const g = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {
        const d = g.call(this, x, y, w, h), c = new Uint8ClampedArray(d.data);
        for (let i = 0; i < c.length; i += 4) { c[i] ^= 1; c[i+1] ^= 1; c[i+2] ^= 1; }
        d.data.set(c); return d;
    };
    WebGLRenderingContext.prototype.getParameter = (function(orig) {
        return function(p) {
            if (p === 37445) return 'Intel Inc.';
            if (p === 37446) return 'Intel Iris OpenGL Engine';
            return orig.call(this, p);
        };
    })(WebGLRenderingContext.prototype.getParameter);
    Object.defineProperty(navigator, 'plugins', { get: () => {
        const p = [{name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},{name:'Native Client'}];
        p.item = (i) => p[i]; return p;
    }});
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
    const __fc = navigator.hardwareConcurrency;
    Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => Math.max(4, Math.min(8, __fc || 8)) });
})();
"""

    @classmethod
    async def get_script(cls) -> str:
        if cls._script is not None:
            return cls._script
        try:
            path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "scripts", "anti_fingerprint.js",
            )
            if os.path.exists(path):
                with open(path) as f:
                    cls._script = f.read()
                    return cls._script
        except Exception:
            pass
        cls._script = cls._fallback
        return cls._script


def _detect_sandbox() -> bool:
    """Detect whether browser sandbox should be enabled."""
    is_root = os.geteuid() == 0 if hasattr(os, "geteuid") else False
    in_container = os.path.exists("/.dockerenv")
    return not (is_root or in_container)


def _find_playwright_chromium() -> Optional[str]:
    """Locate the Playwright-installed Chromium binary.

    In Docker there is no system Chrome, so nodriver would otherwise fail or
    download its own browser. Reuse the Chromium installed via
    `playwright install` (PLAYWRIGHT_BROWSERS_PATH).
    """
    pw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(
        os.path.expanduser("~"), ".cache", "ms-playwright"
    )
    try:
        import glob
        for p in glob.glob(os.path.join(pw, "chromium-*", "chrome-linux64", "chrome")):
            if os.path.exists(p):
                return p
    except Exception:
        pass
    return None


# =============================================================================
# Internal: StealthBrowserClient — connects to existing stealth-browser HTTP
# service. No MCP, no bridge module, purely internal to this file.
# =============================================================================

class StealthBrowserClient(BaseLibPlusStrategy):
    """
    Connects to the existing stealth-browser nodriver service via HTTP REST.
    NO MCP protocol — uses raw HTTP.
    NO separate bridge module — internal to nodriver_strategy.py.

    Endpoints used (from stealth-browser's FastMCP on HTTP transport):
      GET  /health
      POST /tools/call  (with name=spawn_browser, navigate, etc.)

    Since the existing stealth-browser uses FastMCP, we call the MCP
    HTTP transport tools/call endpoint directly.
    """

    def __init__(self, base_url: str = "", timeout: float = 120.0):
        self._base_url = base_url or os.getenv("STEALTH_BROWSER_URL", "http://stealth-browser:8000")
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._anti_fp = ""
        self._available = False

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.NODRIVER

    async def initialize(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        self._anti_fp = await AntiFingerprintLoader.get_script()
        try:
            r = await self._client.get(f"{self._base_url}/health", timeout=5.0)
            self._available = r.status_code == 200
        except Exception:
            self._available = False
        if self._available:
            logger.info(f"StealthBrowserClient: connected to {self._base_url}")
        else:
            logger.warning(f"StealthBrowserClient: {self._base_url} unreachable")

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()

    async def fetch(self, request: FetchRequest) -> FetchResult:
        """Fetch via existing stealth-browser HTTP service (no MCP)."""
        start = time.monotonic()
        if not self._available or not self._client:
            return self._make_result(request, start, success=False, error="stealth-browser unavailable")

        domain = urlparse(request.url).netloc
        instance_id = f"lp_{int(time.monotonic()*1000)}"

        try:
            # 1. Spawn via MCP tools/call endpoint
            spawn_res = await self._client.post(f"{self._base_url}/tools/call", json={
                "name": "spawn_browser",
                "arguments": {
                    "instance_id": instance_id,
                    "headless": True,
                    "viewport_width": 1920,
                    "viewport_height": 1080,
                    "block_resources": ["image", "media", "font", "stylesheet"],
                },
            }, timeout=30.0)
            if spawn_res.status_code not in (200, 201):
                return self._make_result(request, start, success=False, error=f"spawn failed {spawn_res.status_code}")

            # 2. Inject anti-fingerprint
            if self._anti_fp:
                await self._client.post(f"{self._base_url}/tools/call", json={
                    "name": "execute_script",
                    "arguments": {"instance_id": instance_id, "script": self._anti_fp},
                }, timeout=10.0)

            # 3. Set shared cookies
            shared = await cross_strategy_jar.export_to_browser(domain)
            if shared:
                for c in shared:
                    c["url"] = request.url
                    await self._client.post(f"{self._base_url}/tools/call", json={
                        "name": "set_cookie",
                        "arguments": {"instance_id": instance_id, **c},
                    }, timeout=5.0)

            # 4. Navigate
            nav_res = await self._client.post(f"{self._base_url}/tools/call", json={
                "name": "navigate",
                "arguments": {
                    "instance_id": instance_id, "url": request.url,
                    "wait_until": "networkidle" if "google." in request.url else "load",
                    "timeout": int(request.timeout * 1000),
                },
            }, timeout=request.timeout + 10)
            if nav_res.status_code != 200:
                return self._make_result(request, start, success=False, error="navigate failed")

            nav_data = nav_res.json()
            final_url = nav_data.get("result", {}).get("content", {}).get("url", "") or request.url
            await asyncio.sleep(0.5)

            # 5. Extract clean DOM
            clean_js = """
            (() => {
                const c = document.documentElement.cloneNode(true);
                c.querySelectorAll('script,noscript,iframe,style,link[rel=stylesheet]').forEach(e => e.remove());
                c.querySelectorAll('[hidden],[style*=\"display:none\"],[style*=\"display: none\"]').forEach(e => e.remove());
                return c.outerHTML;
            })();
            """
            dom_res = await self._client.post(f"{self._base_url}/tools/call", json={
                "name": "execute_script",
                "arguments": {"instance_id": instance_id, "script": clean_js},
            }, timeout=15.0)
            html = ""
            if dom_res.status_code == 200:
                html = dom_res.json().get("result", {}).get("content", {}).get("result", "")

            # 6. Extract cookies
            cook_res = await self._client.post(f"{self._base_url}/tools/call", json={
                "name": "get_cookies",
                "arguments": {"instance_id": instance_id},
            }, timeout=10.0)
            cookies = {}
            if cook_res.status_code == 200:
                raw = cook_res.json().get("result", {}).get("content", [])
                if isinstance(raw, list):
                    for c in raw:
                        if isinstance(c, dict) and "name" in c:
                            cookies[c["name"]] = c.get("value", "")
            if cookies:
                await cross_strategy_jar.set_cookies_batch(domain, cookies, source_strategy="stealth_client")

            # 7. Close
            await self._client.post(f"{self._base_url}/tools/call", json={
                "name": "close_instance", "arguments": {"instance_id": instance_id},
            }, timeout=5.0)

            return self._make_result(request, start, success=len(html) > 200, status_code=200,
                                      final_url=final_url, html=html, cookies=cookies,
                                      metadata={"via": "stealth_internal"})
        except httpx.TimeoutException:
            return self._make_result(request, start, success=False, error="stealth timeout")
        except Exception as e:
            return self._make_result(request, start, success=False, error=str(e))


# =============================================================================
# Internal: NodriverPool — spawn/manage direct nodriver instances
# =============================================================================

class NodriverPool:
    """Pool of reusable nodriver browser instances with full anti-fingerprinting."""

    def __init__(self, max_instances: int = 5, idle_timeout: int = 300):
        self._max = max_instances
        self._idle_to = idle_timeout
        self._instances: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._sandbox = _detect_sandbox()
        self._anti_fp: Optional[str] = None

    async def _load_afp(self) -> str:
        if self._anti_fp is None:
            self._anti_fp = await AntiFingerprintLoader.get_script()
        return self._anti_fp

    async def acquire(self, headless: bool = True, proxy: Optional[str] = None) -> tuple[str, Any, Any]:
        import nodriver as uc
        async with self._lock:
            for iid, d in self._instances.items():
                # Proxy is fixed at launch, so an instance started with a
                # different proxy can't serve this request.
                if (not d.get("in_use")
                        and d.get("headless") == headless
                        and d.get("proxy") == proxy):
                    b = d["browser"]
                    t = b.main_tab if b else None
                    if t:
                        d["in_use"] = True; d["last_used"] = time.monotonic()
                        return iid, b, t
            if len(self._instances) >= self._max:
                oldest = min(self._instances, key=lambda k: self._instances[k]["last_used"])
                await self._close(oldest)

            iid = f"lp_{int(time.monotonic()*1000)}"
            pw_chromium = _find_playwright_chromium()
            if pw_chromium:
                config = uc.Config(
                    headless=headless, sandbox=self._sandbox,
                    browser_executable_path=pw_chromium,
                )
            else:
                config = uc.Config(headless=headless, sandbox=self._sandbox)
            # Container/bot hardening — hide internal IP via WebRTC, avoid
            # headless chrome artifacts that anti-bot JS fingerprints.
            config.add_argument("--no-first-run")
            config.add_argument("--no-default-browser-check")
            config.add_argument("--disable-background-networking")
            config.add_argument("--disable-component-update")
            config.add_argument("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
            config.add_argument("--lang=en-US")
            # Never launch with the headless default UA ("HeadlessChrome/xxx" is
            # an instant bot giveaway). Pin the persona UA (matches the GUI
            # Chrome + every HTTP strategy) so all paths present one identity.
            config.add_argument(
                "--user-agent=" + os.getenv(
                    "PERSONA_UA",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/146.0.0.0 Safari/537.36",
                )
            )
            if proxy:
                config.add_argument(f"--proxy-server={proxy}")
            browser = await uc.start(config=config)
            tab = browser.main_tab
            try:
                # Register the anti-fingerprint script so it runs on EVERY new
                # document (tab.evaluate would only run once and be wiped on
                # navigate → target page sees zero spoofing). Must call
                # page.enable() first or the script registers but never runs
                # (nodriver quirk — matches nodriver's own _prepare_expert).
                await tab.send(uc.cdp.page.enable())
                await tab.send(
                    uc.cdp.page.add_script_to_evaluate_on_new_document(
                        source=await self._load_afp()
                    )
                )
                # Emulate a realistic timezone — container TZ=UTC is a giveaway.
                await tab.send(uc.cdp.emulation.set_timezone_override(
                    timezone_id="Asia/Dhaka",
                ))
            except Exception:
                pass
            self._instances[iid] = {"browser": browser, "tab": tab, "in_use": True,
                                     "headless": headless, "proxy": proxy,
                                     "created_at": time.monotonic(), "last_used": time.monotonic()}
            return iid, browser, tab

    async def release(self, iid: str) -> None:
        async with self._lock:
            d = self._instances.get(iid)
            if d:
                d["in_use"] = False; d["last_used"] = time.monotonic()

    async def _close(self, iid: str) -> None:
        d = self._instances.pop(iid, None)
        if d:
            try:
                if d.get("browser"): await d["browser"].stop()
            except Exception:
                pass

    async def cleanup(self) -> int:
        async with self._lock:
            now = time.monotonic()
            expired = [i for i, d in self._instances.items()
                       if not d.get("in_use") and now - d.get("last_used", 0) > self._idle_to]
            for i in expired:
                await self._close(i)
            return len(expired)

    async def shutdown(self) -> None:
        async with self._lock:
            for i in list(self._instances):
                await self._close(i)


class NodriverStrategy(BaseLibPlusStrategy):
    """
    CDP-direct browser strategy using nodriver.
    Uses direct nodriver instances by default, falls back to
    StealthBrowserClient (internal HTTP to existing service) if needed.
    """

    def __init__(self, pool: Optional[NodriverPool] = None, use_stealth_service: bool = False):
        self._pool = pool or NodriverPool()
        self._stealth = StealthBrowserClient() if use_stealth_service else None
        self._available = False

    @property
    def strategy_type(self) -> StrategyType:
        return StrategyType.NODRIVER

    @property
    def pool(self) -> NodriverPool:
        return self._pool

    async def initialize(self) -> None:
        try:
            import nodriver as uc  # noqa
            self._available = True
            logger.info(f"NodriverStrategy ready (sandbox={self._pool._sandbox})")
        except ImportError:
            if self._stealth:
                await self._stealth.initialize()
                self._available = self._stealth._available
            if not self._available:
                logger.warning("nodriver not available — try: pip install nodriver")

    async def shutdown(self) -> None:
        await self._pool.shutdown()
        if self._stealth:
            await self._stealth.shutdown()

    async def fetch(self, request: FetchRequest) -> FetchResult:
        start = time.monotonic()
        if not self._available:
            return self._make_result(request, start, success=False, error="nodriver unavailable")

        import nodriver as uc
        iid = ""
        try:
            iid, browser, tab = await self._pool.acquire(headless=True, proxy=request.proxy_url)
            domain = urlparse(request.url).netloc

            # Share cookies
            shared = await cross_strategy_jar.get_cookies(domain)
            if shared:
                for n, v in shared.items():
                    try:
                        await tab.send(uc.cdp.network.set_cookie(
                            name=n, value=v, domain=domain, path="/", secure=True, httpOnly=False,
                        ))
                    except Exception:
                        pass

            # Navigation must have a timeout — tab.get() blocks without one, and a
            # slow/challenged site would hang the whole pipeline.
            await asyncio.wait_for(
                tab.get(request.url), timeout=request.timeout + 5
            )
            await asyncio.sleep(2)

            html = await tab.evaluate("document.documentElement.outerHTML") or ""
            final_url = await tab.evaluate("window.location.href") or request.url

            try:
                cr = await tab.send(uc.cdp.network.get_cookies())
                cookies = {}
                if isinstance(cr, dict):
                    for c in cr.get("cookies", []):
                        cookies[c["name"]] = c["value"]
                elif isinstance(cr, list):
                    for c in cr:
                        cookies[c.get("name", "")] = c.get("value", "")
            except Exception:
                cookies = {}

            if cookies:
                await cross_strategy_jar.set_cookies_batch(domain, cookies, source_strategy="nodriver")

            return self._make_result(request, start, success=len(html) > 200, status_code=200,
                                      final_url=final_url, html=html, cookies=cookies)
        except Exception as e:
            return self._make_result(request, start, success=False, error=str(e))
        finally:
            if iid:
                await self._pool.release(iid)
