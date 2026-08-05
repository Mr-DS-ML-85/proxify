"""
Playwright Strategy — Fresh browser per Google request to avoid persistent state corruption.
Re-launches browser if it crashes between requests.
"""

import asyncio
import logging
import random
import time
from typing import Optional
from urllib.parse import urlparse

from strategies.base import BaseStrategy, FetchRequest, FetchResult
from services.cookie_jar import CookieManager
from services.ua_pool import ua_pool
from engine.dom_cleaner import clean_dom, make_google_results_markdown

logger = logging.getLogger(__name__)

_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1280, "height": 720},
]

_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Los_Angeles",
    "America/Denver", "Europe/London", "Europe/Berlin",
    "Europe/Paris", "Asia/Tokyo", "Asia/Shanghai", "Australia/Sydney",
    "Asia/Dhaka",  # user's locale — avoid the container's default TZ=UTC
]

_LOCALES = [
    "en-US", "en-GB", "en-CA", "en-AU",
    "de-DE", "fr-FR", "es-ES", "ja-JP",
]

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
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({state: Notification.permission}) :
        originalQuery(parameters)
);
Object.defineProperty(screen, 'width', {get: () => window.innerWidth || 1920});
Object.defineProperty(screen, 'height', {get: () => window.innerHeight || 1080});
Object.defineProperty(screen, 'availWidth', {get: () => window.innerWidth || 1920});
Object.defineProperty(screen, 'availHeight', {get: () => window.innerHeight || 1080});
Object.defineProperty(screen, 'colorDepth', {get: () => 24});
Object.defineProperty(screen, 'pixelDepth', {get: () => 24});

// Hardware — normalize to look like a typical desktop (containers often
// report odd core counts / missing deviceMemory)
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});

// WebGL — spoof the renderer. Headless/container Chromium exposes
// "Google SwiftShader" which is an instant container giveaway.
const __spoofWebGL = (proto) => {
    const orig = proto.getParameter;
    proto.getParameter = function(p) {
        if (p === 37445) return 'Intel Inc.';          // UNMASKED_VENDOR_WEBGL
        if (p === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
        if (p === 7936) return 'WebKit';               // VENDOR
        if (p === 7937) return 'WebKit WebGL';         // RENDERER
        return orig.call(this, p);
    };
};
__spoofWebGL(WebGLRenderingContext.prototype);
if (window.WebGL2RenderingContext) __spoofWebGL(WebGL2RenderingContext.prototype);

// WebRTC — strip local IP candidates. In Docker the internal 172.x/eth0 IP
// would otherwise leak through STUN, fingerprinting the container.
if (window.RTCPeerConnection) {
    const __origSetLocal = RTCPeerConnection.prototype.setLocalDescription;
    RTCPeerConnection.prototype.setLocalDescription = function(desc) {
        if (desc && desc.sdp) {
            desc.sdp = desc.sdp.replace(/a=candidate:.*udp.*\\r\\n/g, '');
        }
        return __origSetLocal.call(this, desc);
    };
    const __origCreateData = RTCPeerConnection.prototype.createDataChannel;
    RTCPeerConnection.prototype.createDataChannel = function() { return null; };
}
"""

# Chromium launch args hardened against container/bot detection.
# NOTE: --disable-gpu is intentionally NOT set — disabling GPU forces
# SwiftShader-only rendering which makes canvas/WebGL fingerprints look
# like a headless container. Renderer strings are spoofed via the init script.
_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-default-apps",
    "--disable-extensions",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-first-run",
    "--no-default-browser-check",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--lang=en-US",
]

# Tracks whether we already attempted a self-healing browser install.
_INSTALL_TRIED = False


class PlaywrightStrategy(BaseStrategy):
    """
    Playwright fetcher with anti-detection.
    Creates fresh browser context per request (no persistent state).
    """

    def __init__(self, cookie_manager: CookieManager) -> None:
        self._cookie_manager = cookie_manager
        self._playwright = None
        self._browser = None
        self._available = False
        self._initialized = False
        self._browser_lock = asyncio.Lock()

    STEALTH_INIT_SCRIPT = _STEALTH_INIT_SCRIPT

    @property
    def name(self) -> str:
        return "playwright"

    @property
    def priority(self) -> int:
        return 30

    async def initialize(self) -> None:
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._launch_browser()
            self._available = True
            self._initialized = True
            logger.info("PlaywrightStrategy initialized")
        except Exception as e:
            logger.warning(f"Playwright not available: {e}")
            self._available = False

    async def _ensure_browser_installed(self) -> bool:
        """Self-heal: install the matching Chromium build for this Playwright version.

        Two paths, tried in order:
          1. `python -m playwright install chromium` — works on supported distros
             (e.g. Debian in the Docker image).
          2. Manual Chrome-for-Testing download — bypasses the installer's OS
             whitelist, which refuses newer distros (e.g. "Playwright does not
             support chromium on ubuntu26.04-x64"). Uses the exact build the
             installed playwright driver expects.
        """
        global _INSTALL_TRIED
        if _INSTALL_TRIED:
            return False
        _INSTALL_TRIED = True
        try:
            import sys
            logger.warning(
                "Playwright browser missing — running 'python -m playwright "
                "install chromium'..."
            )
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playwright", "install", "chromium",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            await asyncio.wait_for(proc.communicate(), timeout=600)
            if proc.returncode == 0:
                logger.info("Playwright Chromium auto-installed successfully")
                return True
            logger.warning(
                f"playwright install exited with code {proc.returncode} — "
                "falling back to manual Chrome-for-Testing download"
            )
        except Exception as e:
            logger.warning(f"Playwright auto-install failed: {e}")
        return await asyncio.to_thread(self._manual_download_browsers)

    def _manual_download_browsers(self) -> bool:
        """Manual Chrome-for-Testing install (sync, runs in a worker thread).

        Reads the exact build the installed playwright driver expects from
        browsers.json, then downloads chromium + chrome-headless-shell from
        Playwright's CFT CDN straight into the browsers cache directory.
        """
        import json
        import os
        import shutil
        import urllib.request
        import zipfile

        import playwright

        try:
            cache_dir = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.join(
                os.path.expanduser("~"), ".cache", "ms-playwright"
            )
            browsers_json = os.path.join(
                os.path.dirname(playwright.__file__), "driver", "package", "browsers.json"
            )
            with open(browsers_json) as f:
                data = json.load(f)
            bs = data["browsers"] if isinstance(data, dict) else data
            chrom = next(b for b in bs if b["name"] == "chromium")
            revision = chrom["revision"]
            version = chrom["browserVersion"]
        except Exception as e:
            logger.warning(f"Cannot resolve browser build from browsers.json: {e}")
            return False

        tmp = os.path.join(cache_dir, ".cft_tmp")
        os.makedirs(tmp, exist_ok=True)

        def _fetch(url: str, dest: str) -> bool:
            try:
                logger.info(f"Downloading {url}")
                with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as out:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                return True
            except Exception as e:
                logger.warning(f"CFT download failed for {url}: {e}")
                return False

        entries = [
            (f"chromium-{revision}", "chrome-linux64.zip", "chrome-linux64", "chrome"),
            (f"chromium_headless_shell-{revision}", "chrome-headless-shell-linux64.zip",
             "chrome-headless-shell-linux64", "chrome-headless-shell"),
        ]
        ok = True
        for folder, suffix, inner, exe in entries:
            dest_dir = os.path.join(cache_dir, folder)
            if os.path.exists(os.path.join(dest_dir, inner, exe)):
                continue
            zip_path = os.path.join(tmp, suffix)
            url = f"https://cdn.playwright.dev/builds/cft/{version}/linux64/{suffix}"
            if not _fetch(url, zip_path):
                ok = False
                continue
            try:
                os.makedirs(dest_dir, exist_ok=True)
                with zipfile.ZipFile(zip_path) as z:
                    z.extractall(dest_dir)
                logger.info(f"Extracted {suffix} -> {dest_dir}")
            except Exception as e:
                logger.warning(f"Extract failed for {suffix}: {e}")
                ok = False
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass
        if ok:
            logger.info("Manual Chrome-for-Testing install complete")
        return ok

    async def _launch_browser(self):
        """Launch Chromium with hardened stealth args; retries once after a
        self-healing browser install if the executable is missing."""
        from playwright.async_api import async_playwright
        try:
            return await self._playwright.chromium.launch(
                headless=True,
                args=_CHROMIUM_ARGS,
            )
        except Exception as e:
            msg = str(e)
            if ("Executable doesn't exist" in msg or "playwright install" in msg
                    or "Looks like Playwright was just installed" in msg):
                if await self._ensure_browser_installed():
                    return await self._playwright.chromium.launch(
                        headless=True,
                        args=_CHROMIUM_ARGS,
                    )
            raise

    async def get_browser(self):
        if not self._initialized:
            await self.initialize()
        async with self._browser_lock:
            return self._browser

    async def _ensure_browser(self) -> bool:
        try:
            return await asyncio.wait_for(
                self._ensure_browser_with_lock(), timeout=10
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.error("Browser lock timeout in _ensure_browser")
            return False

    async def _ensure_browser_with_lock(self) -> bool:
        async with self._browser_lock:
            return await self._ensure_browser_locked()

    async def _ensure_browser_locked(self) -> bool:
        if not self._browser:
            return False
        try:
            connected = await asyncio.wait_for(
                self._browser.is_connected(), timeout=0.5
            )
            if connected:
                return True
        except Exception:
            pass
        logger.warning("Browser dead — re-launching")
        return await self._reinitialize_browser_locked()

    async def _reinitialize_browser_locked(self) -> bool:
        try:
            await self._playwright.stop()
        except Exception:
            pass
        try:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._launch_browser()
            self._available = True
            logger.info("Browser re-launched")
            return True
        except Exception as e:
            logger.error(f"Browser re-launch failed: {e}")
            self._available = False
            return False

    async def fetch(self, request: FetchRequest) -> FetchResult:
        start_time = time.monotonic()

        if not self._available:
            return self._make_result(
                request, start_time,
                success=False, error="Playwright not available",
            )

        if not self._initialized:
            await self.initialize()
            if not self._available:
                return self._make_result(
                    request, start_time,
                    success=False, error="Playwright init failed",
                )

        await self._ensure_browser()

        domain = urlparse(request.url).netloc
        is_google_search = "google." in domain and "/search" in request.url

        context = None
        page = None
        try:
            ua = ua_pool.get_random()
            viewport = random.choice(_VIEWPORTS)
            tz = random.choice(_TIMEZONES)
            locale = random.choice(_LOCALES)

            # Hold lock for entire fetch — prevents concurrent access to
            # the same Playwright browser (avoids Node.js EPIPE crash)
            async with self._browser_lock:
                context = await self._browser.new_context(
                    user_agent=ua, viewport=viewport,
                    locale=locale, timezone_id=tz,
                    permissions=[], java_script_enabled=True,
                    ignore_https_errors=True,
                )

                page = await context.new_page()
                await page.add_init_script(_STEALTH_INIT_SCRIPT)

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
                    _used_fetch = False
                else:
                    current_url = request.url
                    _used_fetch = True

                    async def _override_request(route):
                        if route.request.resource_type == "document":
                            await route.continue_(
                                method=method,
                                post_data=request.body,
                            )
                        else:
                            await route.continue_()

                    try:
                        # page.route() is async and returns an AsyncContextManager
                        async with await page.route(
                            request.url, _override_request
                        ):
                            response = await page.goto(
                                request.url,
                                wait_until="domcontentloaded",
                                timeout=timeout_ms,
                            )
                    except Exception as e:
                        return self._make_result(
                            request, start_time,
                            success=False, status_code=0,
                            final_url=current_url, html="",
                            error=f"Playwright route error: {e}",
                        )

                    current_url = page.url
                    html = await page.content()
                    status_code = response.status if response else 0

                if "/sorry/" in current_url or "captcha" in current_url.lower():
                    logger.warning(f"Google captcha page at {current_url}")
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
                        await page.wait_for_selector("h3", timeout=5000)
                        await asyncio.sleep(random.uniform(0.05, 0.15))
                    except Exception:
                        pass

                # Optional wait: JS-gated content (Yandex CBIR results after
                # an upload/POST, etc.) renders asynchronously.
                waited = False
                if request.wait_selector:
                    try:
                        await page.wait_for_selector(
                            request.wait_selector, timeout=min(12000, int(request.timeout * 1000))
                        )
                        await asyncio.sleep(random.uniform(0.05, 0.15))
                        waited = True
                    except Exception:
                        pass

                await asyncio.sleep(random.uniform(0.05, 0.1))
                if not _used_fetch or waited:
                    html = await page.content()
                else:
                    status_code = 200

                # DOM cleaning for all responses (GET and non-GET)
                if html:
                    cleaned = clean_dom(html, url=request.url)
                    if cleaned.success:
                        html = cleaned.clean_html or ""
                        if cleaned.google_results:
                            logger.info(
                                f"Playwright Google DOM cleaned — "
                                f"{len(cleaned.google_results)} results"
                            )
                else:
                    html = ""

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
                success=False, status_code=0, error="Playwright timeout",
            )
        except Exception as e:
            logger.warning(f"Playwright error: {e}")
            return self._make_result(
                request, start_time,
                success=False, status_code=0, error=str(e),
            )
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

    async def shutdown(self) -> None:
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
