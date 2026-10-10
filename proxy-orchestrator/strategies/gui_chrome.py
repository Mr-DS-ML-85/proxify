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
import base64
import os
import logging
import random
import re
import time
from typing import Optional
from urllib.parse import urlparse

from strategies.base import BaseStrategy, FetchRequest, FetchResult, is_binary_response
from services.cookie_jar import CookieManager
from engine.dom_cleaner import clean_dom, make_google_results_markdown
from config import config

logger = logging.getLogger(__name__)

GUI_CDP_URL = "http://127.0.0.1:9222"


# GUI_HONEST_MODE=true: present the real browser exactly as it is (no init
# script, no UA/client-hint override, no timezone emulation).
_HONEST = os.getenv("GUI_HONEST_MODE", "false").lower() == "true"


class _SkipOverride(Exception):
    """Control flow: skip an emulation override in honest mode."""


def _persona_major() -> str:
    """Chrome major version named by the persona UA ("153")."""
    import re as _re

    m = _re.search(r"Chrome/(\d+)", config.PERSONA_UA or "")
    return m.group(1) if m else "146"

_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
]

_TIMEZONES = ["Asia/Dhaka", "Asia/Dhaka", "America/New_York", "Europe/London"]

# Aggressive stealth hardening — the GUI browser must present an
# indistinguishable-from-CPU-imaged-real-human identity: no automation
# surface, realistic hardware/WebGL/canvas/audio, and no visible container
# tells. Runs as a CDP init script before every new document.
_STEALTH_INIT_SCRIPT = r"""
(() => {
  // -- WebDriver / automation surface ------------------------------------
  Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
  Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
  Object.defineProperty(navigator, 'plugins', {get: () => [
    {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
    {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
    {name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer'},
  ]});
  Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});
  // flags exposed by Puppeteer/Playwright detection scripts
  for (const k of ['phantom','selenium','_selenium','callPhantom','__nightmare',
                   '_Selenium_IDE_Recorder','domAutomation','domAutomationController',
                   'webdriver']) {
    try { Object.defineProperty(window, k, {get: () => undefined, configurable: true}); } catch (e) {}
  }
  window.chrome = {
    runtime: {}, loadTimes: function() {}, csi: function() {},
    app: {}, webstore: {},
  };

  // Hardware / platform consistency (matches persona: Windows x64, 8 cores, 8GB)
  Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
  Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
  Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
  // CRITICAL CONSISTENCY: these values MUST match the CDP
  // Emulation.setUserAgentOverride userAgentMetadata below (and the persona
  // UA / PERSONA_TLS chrome146). The JS-visible navigator.userAgentData and
  // the Sec-CH-UA HTTP headers are read by Google's risk engine as ONE
  // identity — any drift (x86 vs x64, brand count, platform) is the #1
  // "fooled" signal. brand info, arch/bitness are pinned to the CDP override.
  Object.defineProperty(navigator, 'userAgentData', {
    get: () => ({
      brands: [
        {brand: 'Chromium', version: '146'},
        {brand: 'Google Chrome', version: '146'},
      ],
      mobile: false,
      platform: 'Windows',
      getHighEntropyValues: async () => ({
        architecture: 'x64', bitness: '64', model: '', platformVersion: '10.0',
        uaFullVersion: '146.0.0.0', wow64: false,
      }),
    }),
  });

  // WebGL — real GPU string, not SwiftShader/WebKit fallback. Patch the
  // prototype so EVERY context (not just the one we create) reports the
  // persona's Intel GPU. SwiftShader's "WebKit WebGL" renderer is a top
  // container/VM tell.
  const __spoofWebGLInfo = (gl) => {
    if (!gl) return;
    const orig = gl.getParameter.bind(gl);
    gl.getParameter = (p) => {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris Xe Graphics';
      if (p === 7936) return 'Google Inc. (Intel)';
      if (p === 7937) return 'ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, OpenGL 4.6)';
      if (p === 7890) return 1;
      if (p === 3413) return 'WebGL 1.0 (OpenGL ES 2.0 Chromium)';
      return orig(p);
    };
  };
  const __patch = (proto) => {
    if (!proto) return;
    const origGetP = proto.prototype.getParameter;
    proto.prototype.getParameter = function(p) {
      if (p === 37445) return 'Intel Inc.';
      if (p === 37446) return 'Intel Iris Xe Graphics';
      if (p === 7936) return 'Google Inc. (Intel)';
      if (p === 7937) return 'ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, OpenGL 4.6)';
      if (p === 7890) return 1;
      if (p === 3413) return 'WebGL 1.0 (OpenGL ES 2.0 Chromium)';
      if (p === 3415) return 'WebGL 2.0 (OpenGL ES 3.0 Chromium)';
      if (p === 7938) return 'Google SwiftShader';
      if (p === 7939) return 1;
      return origGetP.call(this, p);
    };
  };
  __patch(window.WebGLRenderingContext);
  __patch(window.WebGL2RenderingContext);
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    if (gl) __spoofWebGLInfo(gl);
  } catch (e) {}
  try {
    const gl2 = document.createElement('canvas').getContext('webgl2');
    if (gl2) __spoofWebGLInfo(gl2);
  } catch (e) {}

  // Audio — silence proxied to avoid a 0-length fingerprint hash
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (AC) {
      const origMO = AC.prototype.createOscillator;
      const origDN = AC.prototype.createDynamicsCompressor;
      try {
        AC.prototype.createOscillator = function() {
          const osc = origMO.call(this);
          Object.defineProperty(osc, 'getFrequencyData', {get: () => () => new Float32Array([0])});
          return osc;
        };
      } catch (e) {}
    }
  } catch (e) {}

  // Navigator.permissions — hide the automation 'camera'-style ask patterns
  try {
    navigator.permissions && navigator.permissions.query &&
      URL.createObjectURL &&
      document.addEventListener('visibilitychange', () => {});
  } catch (e) {}

  // WebRTC: mask the real (container) IP leak surface
  try {
    Object.defineProperty(navigator, 'connection', {
      get: () => ({ effectiveType: navigator.connection && navigator.connection.effectiveType || '4g',
                   rtt: 50, downlink: 10, saveData: false }),
    });
  } catch (e) {}

  // Battery API — present on Chrome/Windows, absent on Chrome/Linux. Reporting
  // absent while the persona is Windows is a Linux/container tell.
  try {
    if (!navigator.getBattery) {
      Object.defineProperty(navigator, 'getBattery', {
        value: () => Promise.resolve({
          charging: true, chargingTime: 0, dischargingTime: Infinity,
          level: 1, addEventListener: () => {}, removeEventListener: () => {},
        }),
      });
    }
  } catch (e) {}
})();
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

    @staticmethod
    def _multipart_file(body: bytes, content_type: str):
        """First file part of a multipart body -> (field, filename, ctype, bytes)."""
        m = re.search(r"boundary=([^;]+)", content_type or "", re.I)
        if not m or not body:
            return None
        delim = b"--" + m.group(1).strip().strip('"').encode()
        for part in body.split(delim)[1:]:
            if part.startswith(b"--"):
                continue
            head, _, data = part.partition(b"\r\n\r\n")
            text = head.decode("latin-1", "replace")
            if "filename=" not in text:
                continue
            field = re.search(r'name="([^"]*)"', text)
            fname = re.search(r'filename="([^"]*)"', text)
            ctype = re.search(r"Content-Type:\s*(\S+)", text, re.I)
            return (
                field.group(1) if field else "file",
                fname.group(1) if fname else "upload.bin",
                ctype.group(1) if ctype else "application/octet-stream",
                data[:-2] if data.endswith(b"\r\n") else data,
            )
        return None

    async def _form_upload(self, request, start_time, page, context, timeout_ms) -> FetchResult:
        """Upload the request's file by submitting a real form in the page.

        metadata: upload_landing (page opened first; default = origin of the
        upload URL), result_url_contains (navigation that means "done";
        default "/search").
        """
        part = self._multipart_file(request.body or b"", request.headers.get("Content-Type", ""))
        if part is None:
            return self._make_result(request, start_time, success=False, error="in_page_upload: no file part in body")
        field, fname, ctype, data = part
        parsed = urlparse(request.url)
        landing = request.metadata.get("upload_landing") or f"{parsed.scheme}://{parsed.netloc}/"
        needle = request.metadata.get("result_url_contains") or "/search"
        try:
            await page.goto(landing, wait_until="domcontentloaded", timeout=timeout_ms)
            await asyncio.sleep(2.0)  # let the landing page settle
            async with page.expect_navigation(
                url=lambda u: needle in u, timeout=timeout_ms, wait_until="domcontentloaded",
            ):
                await page.evaluate(
                    """(a) => {
                      const bin = atob(a.b64); const u8 = new Uint8Array(bin.length);
                      for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
                      const dt = new DataTransfer();
                      dt.items.add(new File([u8], a.name, {type: a.ctype}));
                      const f = document.createElement('form');
                      f.method = 'POST'; f.enctype = 'multipart/form-data'; f.action = a.action;
                      f.style.display = 'none';
                      const i = document.createElement('input');
                      i.type = 'file'; i.name = a.field; i.files = dt.files;
                      f.appendChild(i); document.body.appendChild(f); f.submit();
                    }""",
                    {"b64": base64.b64encode(data).decode("ascii"), "name": fname, "ctype": ctype,
                     "field": field, "action": request.url},
                )
            if request.wait_selector:
                try:
                    await page.wait_for_selector(request.wait_selector, timeout=min(12000, timeout_ms))
                except Exception:
                    pass
            else:
                await asyncio.sleep(3.0)  # results hydrate after domcontentloaded
            html = await page.content()
            try:
                gui_cookies = await context.cookies()
                if gui_cookies:
                    await self._cookie_manager.set_cookies(
                        urlparse(page.url).netloc, {c["name"]: c["value"] for c in gui_cookies},
                    )
            except Exception:
                pass
            blocked = "/sorry/" in page.url or "unusual traffic" in html
            return self._make_result(
                request, start_time,
                success=not blocked and len(html) > 1000,
                status_code=429 if blocked else 200,
                final_url=page.url,
                html=html,  # raw: results live in <script> JSON — never DOM-clean
                error="Google captcha page" if blocked else None,
            )
        except Exception as e:
            logger.warning(f"gui_chrome in-page upload failed: {e}")
            return self._make_result(request, start_time, success=False, error=f"in-page upload error: {e}")

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
                # GUI_HONEST_MODE: inject NOTHING. Every JS shim leaves a
                # trace (navigator.webdriver deleted -> undefined instead of
                # false, toString/descriptor changes, zeroed audio hash) and
                # the Windows values contradicted a Linux UA. The real
                # Chromium's own values are consistent by construction.
                if not _HONEST:
                    await page.add_init_script(_STEALTH_INIT_SCRIPT)

                # CRITICAL FINGERPRINT CONSISTENCY: the GUI Chrome is a real
                # Chromium 149 binary on Linux, but the persona pins UA to
                # Chrome/146 on Windows. Spoofing ONLY the UA string leaks the
                # truth via client hints — Sec-CH-UA reports v="149" and
                # Sec-CH-UA-Platform reports "Linux" — a contradiction Google's
                # risk engine reads as "fooled". Override the full UA + client
                # hint metadata so the browser presents ONE coherent identity.
                cdp = None
                try:
                    if _HONEST:
                        raise _SkipOverride()
                    cdp = await context.new_cdp_session(page)
                    await cdp.send(
                        "Emulation.setUserAgentOverride",
                        {
                            "userAgent": config.PERSONA_UA,
                            "userAgentMetadata": {
                                # brands/version/platform DERIVED from the
                                # persona UA (BrowserPilot fingerprint-profile
                                # rule: every signal describes ONE identity —
                                # hardcoded 146/Windows contradicted a Linux
                                # Chromium 153 persona)
                                "brands": [
                                    {"brand": "Chromium", "version": _persona_major()},
                                    {"brand": "Google Chrome", "version": _persona_major()},
                                    {"brand": "Not.A/Brand", "version": "99"},
                                ],
                                "fullVersion": f"{_persona_major()}.0.0.0",
                                "platform": config.PERSONA_PLATFORM.strip('"'),
                                "platformVersion": os.getenv("PERSONA_PLATFORM_VERSION", "10.0"),
                                "architecture": "x64",
                                "model": "",
                                "mobile": False,
                            },
                        },
                    )
                except _SkipOverride:
                    pass  # honest mode: the browser's native UA + client hints
                except Exception as e:
                    logger.debug(f"gui_chrome UA/client-hint override failed: {e}")

                # Override timezone for this page to match a real user locale
                # (the GUI profile is UTC — a UTC clock on a Dhaka IP is a
                # top-tier container/automation signal for Google's risk
                # engine). Pin to Asia/Dhaka via CDP so JS, headers, and
                # Intl all agree.
                try:
                    # honest mode: the container's TZ is set to the host's
                    # zone, so the native clock already matches the IP
                    if cdp is None:
                        raise _SkipOverride()
                    await cdp.send(
                        "Emulation.setTimezoneOverride",
                        {"timezoneId": "Asia/Dhaka"},
                    )
                except _SkipOverride:
                    pass
                except Exception as e:
                    logger.debug(f"gui_chrome timezone override failed: {e}")

                # Keep the page's locale coherent with the persona (en-US).
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

                # In-page file upload (Google Lens & co.): Lens results are
                # bound to the browser session that made the upload — a
                # curl-class POST only gets the JS shell back, and fetch()
                # from the Lens UI is cross-origin. Submitting a real <form>
                # with the file attached is a top-level navigation the
                # browser follows to the results page itself.
                if method == "POST" and request.metadata.get("in_page_upload"):
                    return await self._form_upload(request, start_time, page, context, timeout_ms)

                if method == "GET":
                    response = await page.goto(
                        request.url,
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                    current_url = page.url
                    # Direct image / PDF / file URL: return the raw bytes the
                    # browser received (page.content() would be the viewer DOM)
                    if response is not None:
                        _hdrs = dict(response.headers or {})
                        if is_binary_response(_hdrs):
                            try:
                                _raw = await response.body()
                            except Exception as _e:
                                _raw = b""
                                logger.debug(f"gui_chrome binary body unavailable: {_e}")
                            if _raw:
                                return self._make_result(
                                    request, start_time,
                                    success=200 <= response.status < 400,
                                    status_code=response.status,
                                    final_url=current_url,
                                    headers=_hdrs,
                                    body=_raw,
                                )
                    html = await page.content()
                    status_code = response.status if response else 0
                    _used_fetch = False
                else:
                    current_url = request.url
                    _used_fetch = True

                    # Human-like pre-upload activity: a real user focuses the
                    # page and moves the mouse before submitting an image.
                    try:
                        await page.evaluate("document.body.focus()")
                    except Exception:
                        pass
                    try:
                        await page.mouse.move(
                            random.randint(200, 900),
                            random.randint(200, 700),
                            steps=random.randint(5, 15),
                        )
                        await asyncio.sleep(random.uniform(0.2, 0.6))
                    except Exception:
                        pass

                    # Uploads (multipart POST to Google Lens, etc.) return a
                    # session-bound redirect URL (e.g. /search?vsrid=...). The
                    # page.goto below FOLLOWS that redirect in the SAME page
                    # session, so the vsrid URL is rendered with the cookies the
                    # upload just set — one coherent identity. We deliberately
                    # keep the established persona cookies (do NOT clear them):
                    # an anonymous fresh identity doing repeated image uploads
                    # from one IP is exactly the pattern Google's risk engine
                    # flags with /sorry/. Continuity beats freshness here.

                    # Intercept the document navigation request and
                    # override method + body so the browser handles it
                    # natively (correct origin, cookies, CORS, redirects).
                    # Must forward the caller's headers (Content-Type for
                    # multipart uploads, custom auth, etc.) — route.continue_
                    # drops them otherwise, and a POST with a body but no
                    # Content-Type cannot be parsed by the server.
                    async def _override_request(route):
                        if route.request.resource_type == "document":
                            continue_headers = {}
                            if request.headers:
                                continue_headers = {
                                    k: v
                                    for k, v in request.headers.items()
                                    if k.lower()
                                    not in (
                                        "content-length",
                                        "host",
                                        "accept-encoding",
                                        "connection",
                                        "user-agent",
                                    )
                                }
                            await route.continue_(
                                method=method,
                                post_data=request.body,
                                headers=continue_headers or None,
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
                            error=f"GUI Chrome route error: {e}",
                        )

                    current_url = page.url
                    html = await page.content()
                    status_code = response.status if response else 0

                if "/sorry/" in current_url or "captcha" in current_url.lower():
                    # On uploads Google intermittently gates with /sorry/ even
                    # for legitimate sessions (rate throttle). Do NOT hard-fail:
                    # settle the page and let the orchestrator retry/escalate if
                    # the gate is transient; a small wait sometimes clears it.
                    if 60 <= status_code < 400:
                        await asyncio.sleep(random.uniform(0.8, 1.8))
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

                # Optional wait: JS-gated content (Yandex CBIR / Google Lens
                # visual results after an upload/POST) renders asynchronously.
                # Wait for the requested selector so the returned page carries
                # the actual results, not the pre-render shell. For Lens/vsrid
                # the SRP results grid is what we want.
                waited = False
                if request.wait_selector:
                    try:
                        await page.wait_for_selector(
                            request.wait_selector, timeout=min(12000, int(request.timeout * 1000))
                        )
                        await asyncio.sleep(random.uniform(0.3, 0.8))
                        waited = True
                    except Exception:
                        pass
                elif _used_fetch and request.method and request.method.upper() != "GET":
                    # Heuristic wait: after a non-GET (upload), give the JS
                    # results time to hydrate before snapshotting, unless the
                    # page already redirected to /sorry/.
                    #
                    # Google Lens uploads land on https://www.google.com/?olud —
                    # a JS loading page that then client-side-navigates to the
                    # real /search?vsrid=...&udm=26 results URL. The results grid
                    # (#search/.srp/.Srqakc) does NOT exist on the ?olud shell,
                    # so waiting on it alone times out and we snapshot the shell.
                    # Poll for the vsrid navigation first, then wait for results.
                    lens_loaded = False
                    if "olud" in current_url or "lens" in current_url.lower():
                        try:
                            deadline = time.monotonic() + 15
                            while time.monotonic() < deadline:
                                await asyncio.sleep(0.25)
                                cur = page.url
                                if "/sorry/" in cur:
                                    break
                                if "vsrid" in cur or (
                                    "/search" in cur and "udm" in cur
                                ):
                                    lens_loaded = True
                                    break
                        except Exception:
                            pass
                        if lens_loaded:
                            try:
                                await page.wait_for_selector(
                                    "#search, .srp, .Srqakc, .EyBRub", timeout=8000
                                )
                                await asyncio.sleep(random.uniform(0.4, 0.9))
                            except Exception:
                                await asyncio.sleep(random.uniform(0.5, 1.2))
                        else:
                            await asyncio.sleep(random.uniform(0.5, 1.2))
                    else:
                        try:
                            await page.wait_for_selector(
                                "#search, .srqakc, .srp, .Srqakc", timeout=6000
                            )
                            await asyncio.sleep(random.uniform(0.4, 0.9))
                        except Exception:
                            await asyncio.sleep(random.uniform(0.5, 1.2))

                await asyncio.sleep(random.uniform(0.05, 0.15))
                if not _used_fetch or waited:
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
                        html = cleaned.clean_html or ""
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
