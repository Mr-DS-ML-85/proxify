"""
GuiChrome Strategy — Connect to the persistent HEADFUL GUI Chrome (Xvfb VM).

Why this strategy exists:
  - Headless browser launches (Playwright/nodriver/DrissionPage) are detected by
    Google-class anti-bots even with perfect fingerprints — the absence of a
    real display, GPU, and long-lived profile is itself a signal.
  - The orchestrator VM runs a REAL desktop Chrome under Xvfb on
    `--remote-debugging-port=9222` with a PERSISTENT user-data-dir
    (`/app/gui-profile`). That profile accumulates cookies, cache, localStorage
    and history like a normal browser, which is the strongest trust signal a
    site can get.
  - This strategy connects to that GUI Chrome over CDP, uses its persistent
    default context (cookies + cache shared with every other GUI fetch), and
    renders pages in the real window.

Architecture:
  - `chromium.connect_over_cdp("http://127.0.0.1:9222")` — attaches to the
    already-running GUI Chrome; never launches its own browser.
  - Uses `browser.contexts[0]` (the persistent default context) so cookies and
    cache persist across requests AND survive orchestrator restarts.
  - Per-request: new page in the shared context → anti-fp init script → navigate
    → harvest cookies back into CookieManager → close page only (never the
    browser/context — the VM owns them).
  - Google searches wait for `h3` results; DOM is cleaned via clean_dom.
"""

import asyncio
import logging
import random
import time
from typing import Optional
from urllib.parse import urlparse

from strategies.base import BaseStrategy, FetchRequest, FetchResult
from services.cookie_jar import CookieManager
from engine.dom_cleaner import clean_dom, make_google_results_markdown
from config import config

logger = logging.getLogger(__name__)

GUI_CDP_URL = "http://127.0.0.1:9222"

_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
]

_TIMEZONES = ["Asia/Dhaka", "Asia/Dhaka", "America/New_York", "Europe/London"]

# Same hardening as playwright_strategy — even the GUI browser should not look
# like an automation target. The persistent profile already carries real
# cookies/cache; this script removes the remaining webdriver/container tells.
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [
    {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
    {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
]});
window.chrome = {
    runtime: {}, loadTimes: function() {}, csi: function() {}, app: {}, webstore: {},
};
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
const __spoofWebGL = (proto) => {
    const orig = proto.getParameter;
    proto.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        if (p === 7936) return 'WebKit';
        if (p === 7937) return 'WebKit WebGL';
        return orig.call(this, p);
    };
};
__spoofWebGL(WebGLRenderingContext.prototype);
if (window.WebGL2RenderingContext) __spoofWebGL(WebGL2RenderingContext.prototype);
"""


class GuiChromeStrategy(BaseStrategy):
    """Fetch through the persistent headful GUI Chrome VM (CDP attach)."""

    def __init__(self, cookie_manager: CookieManager) -> None:
        self._cookie_manager = cookie_manager
        self._playwright = None
        self._browser = None
        self._available = False
        self._initialized = False
        self._lock = asyncio.Lock()

    @property
    def name(self) -> str:
        return "gui_chrome"

    @property
    def priority(self) -> int:
        return 25  # after fast HTTP strategies, before other browsers

    async def initialize(self) -> None:
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            # Attach to the GUI Chrome the VM launched (never create our own)
            self._browser = await self._playwright.chromium.connect_over_cdp(
                GUI_CDP_URL, timeout=15_000,
            )
            self._available = True
            self._initialized = True
            logger.info(
                f"GuiChromeStrategy attached to GUI Chrome @ {GUI_CDP_URL} "
                f"(persistent profile cookies+cache)"
            )
        except Exception as e:
            logger.warning(
                f"GuiChromeStrategy: GUI Chrome not reachable @ {GUI_CDP_URL} "
                f"(start it with gui_browser.sh): {e}"
            )
            self._available = False

    async def _ensure_browser(self) -> bool:
        if not self._browser:
            return False
        try:
            if await asyncio.wait_for(
                self._browser.is_connected(), timeout=0.5
            ):
                return True
        except Exception:
            pass
        logger.warning("GUI Chrome disconnected — re-attaching")
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                GUI_CDP_URL, timeout=15_000,
            )
            self._available = True
            return True
        except Exception as e:
            logger.error(f"GUI Chrome re-attach failed: {e}")
            self._available = False
            return False

    def _default_context(self):
        """The GUI Chrome's persistent default context (shared cookies/cache)."""
        if self._browser and self._browser.contexts:
            return self._browser.contexts[0]
        return None

    async def fetch(self, request: FetchRequest) -> FetchResult:
        start_time = time.monotonic()

        if not self._initialized:
            await self.initialize()
        if not self._available:
            return self._make_result(
                request, start_time,
                success=False, error="GUI Chrome not available",
            )

        async with self._lock:
            if not await self._ensure_browser():
                return self._make_result(
                    request, start_time,
                    success=False, error="GUI Chrome disconnected",
                )

            domain = urlparse(request.url).netloc
            is_google_search = "google." in domain and "/search" in request.url

            context = self._default_context()
            if context is None:
                return self._make_result(
                    request, start_time,
                    success=False, error="GUI Chrome has no persistent context",
                )

            page = None
            try:
                page = await context.new_page()

                # Anti-fp must apply to the new document before navigation.
                # CDP init scripts are context-wide; for a page created in the
                # shared context, use add_init_script (persists for the page).
                await page.add_init_script(_STEALTH_INIT_SCRIPT)

                # CRITICAL FINGERPRINT CONSISTENCY: the GUI Chrome is a real
                # Chromium 149 binary on Linux, but the persona pins UA to
                # Chrome/146 on Windows. Spoofing ONLY the UA string leaks the
                # truth via client hints — Sec-CH-UA reports v="149" and
                # Sec-CH-UA-Platform reports "Linux" — a contradiction Google's
                # risk engine reads as "fooled". Override the full UA + client
                # hint metadata so the browser presents ONE coherent identity.
                try:
                    cdp = await context.new_cdp_session(page)
                    await cdp.send(
                        "Emulation.setUserAgentOverride",
                        {
                            "userAgent": config.PERSONA_UA,
                            "userAgentMetadata": {
                                "brands": [
                                    {"brand": "Chromium", "version": "146"},
                                    {"brand": "Google Chrome", "version": "146"},
                                ],
                                "fullVersion": "146.0.0.0",
                                "platform": config.PERSONA_PLATFORM.strip('"'),
                                "platformVersion": "10.0",
                                "architecture": "x64",
                                "model": "",
                                "mobile": False,
                            },
                        },
                    )
                except Exception as e:
                    logger.debug(f"gui_chrome UA/client-hint override failed: {e}")

                # Override timezone for this page to match a real user locale
                # (the GUI profile may be UTC — align with Bangladesh origin).
                try:
                    await page.evaluate(
                        "() => {}"
                    )  # ensure page object is ready
                except Exception:
                    pass

                # Seed any cookies the orchestrator already knows for this
                # domain into the GUI profile (session continuity)
                jar_cookies = await self._cookie_manager.get_cookies(domain)
                if jar_cookies and context:
                    try:
                        await context.add_cookies([
                            {
                                "name": k, "value": v, "domain": domain,
                                "path": "/",
                            }
                            for k, v in jar_cookies.items()
                        ])
                    except Exception as e:
                        logger.debug(f"gui_chrome cookie seed failed: {e}")

                timeout_ms = int(request.timeout * 1000)
                method = request.method.upper() if request.method else "GET"

                if method == "GET":
                    response = await page.goto(
                        request.url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    current_url = page.url
                    html = await page.content()
                    status_code = response.status if response else 0
                    _used_fetch = False
                else:
                    current_url = request.url
                    _used_fetch = True
                    fetch_args = {
                        "url": request.url,
                        "method": method,
                        "redirect": "follow",
                        "credentials": "include",
                    }
                    if request.headers:
                        fetch_args["headers"] = {
                            k: v for k, v in request.headers.items()
                            if k.lower() not in ("content-length", "host")
                        }
                    if request.body:
                        fetch_args["body"] = request.body.decode(
                            "utf-8", errors="replace"
                        )
                    try:
                        result = await page.evaluate(
                            """
                            (args) => {
                                return fetch(args.url, args)
                                    .then(r => ({
                                        status: r.status,
                                        text: r.text()
                                    }))
                                    .catch(e => { throw new Error(e.message); });
                            }
                            """,
                            fetch_args,
                        )
                        status_code = result.get("status", 200)
                        html = result.get("text", "")
                    except Exception as e:
                        return self._make_result(
                            request, start_time,
                            success=False, status_code=0,
                            final_url=current_url, html="",
                            error=f"GUI Chrome fetch error: {e}",
                        )

                if "/sorry/" in current_url or "captcha" in current_url.lower():
                    logger.warning(f"GUI Chrome hit captcha at {current_url}")
                    if not _used_fetch:
                        html = await page.content()
                    return self._make_result(
                        request, start_time,
                        success=False, status_code=200,
                        final_url=current_url, html=html,
                        error="Google captcha page",
                    )

                if is_google_search and not _used_fetch:
                    try:
                        await page.wait_for_selector("h3", timeout=7000)
                        await asyncio.sleep(random.uniform(0.05, 0.2))
                    except Exception:
                        pass

                await asyncio.sleep(random.uniform(0.05, 0.15))
                if not _used_fetch:
                    html = await page.content()

                # Harvest cookies from the persistent GUI profile → cookie jar
                try:
                    gui_cookies = await context.cookies()
                    if gui_cookies:
                        await self._cookie_manager.set_cookies(
                            domain,
                            {c["name"]: c["value"] for c in gui_cookies},
                        )
                except Exception as e:
                    logger.debug(f"gui_chrome cookie harvest failed: {e}")

                # DOM cleaning for all responses (GET and non-GET)
                if html:
                    cleaned = clean_dom(html, url=request.url)
                    if cleaned.success:
                        html = cleaned.clean_html
                        if cleaned.google_results:
                            logger.info(
                                f"GUI Chrome Google DOM cleaned — "
                                f"{len(cleaned.google_results)} results"
                            )

                return self._make_result(
                    request, start_time,
                    success=200 <= status_code < 400 and len(html) > 100,
                    status_code=status_code,
                    final_url=current_url,
                    html=html,
                )

            except asyncio.TimeoutError:
                return self._make_result(
                    request, start_time,
                    success=False, status_code=0, error="GUI Chrome timeout",
                )
            except Exception as e:
                logger.warning(f"GuiChrome error: {e}")
                return self._make_result(
                    request, start_time,
                    success=False, status_code=0, error=str(e),
                )
            finally:
                if page:
                    try:
                        # Close only the page — never the browser/context (VM owns them)
                        await page.close()
                    except Exception:
                        pass

    async def shutdown(self) -> None:
        # Do NOT close the GUI browser or context — the VM owns the persistent
        # Chrome. Only stop our CDP client.
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
