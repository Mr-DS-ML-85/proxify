"""
Captcha Solver Bridge — Detection + solving via ai-captcha-bypass (42 types)
and ai-captcha-rs VM agent (headed Chrome for interactive captchas).

Flow:
  1. Detect captcha type from HTML patterns + response headers
  2. Enterprise anti-bot (Kasada/PerimeterX/Akamai/Imperva): route to enterprise solvers
  3. For known sitekey types (hCaptcha, reCAPTCHA, Turnstile): direct API solve
  4. For visual/interactive types (GeeTest, slider, tile): Playwright screenshot → vision API
  5. For Google reCAPTCHA: interactive Playwright solving
  6. Unknown type: screenshot → /solve/auto (LLM auto-classify)
  7. If bypass API unreachable: delegate to ai-captcha-rs VM agent (headed Chrome)

For Google /sorry/ pages: returns solved=False immediately
(rate-limiting blocks cannot be solved — need different IP/proxy).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from typing import Optional, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from strategies.playwright_strategy import PlaywrightStrategy

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
CAPTCHA_API_URL = os.getenv("CAPTCHA_BYPASS_URL", "http://ai-captcha-bypass:8095")
CAPTCHA_VM_URL  = os.getenv("CAPTCHA_VM_URL", "")          # ai-captcha-rs — optional
CAPTCHA_TIMEOUT = float(os.getenv("CAPTCHA_TIMEOUT", "120"))


# ── Type detection ────────────────────────────────────────────────────────────

# HTML pattern → captcha type (checked in order, first match wins)
_HTML_PATTERNS: list[tuple[str, str]] = [
    # Enterprise anti-bot (must check before generic captcha)
    ("x-kpsdk-ct",          "kasada"),         # Kasada response body injection
    ("kpsdk-",              "kasada"),
    ("_px3",                "perimeterx"),
    ("perimeterx",          "perimeterx"),
    ("px-captcha",          "perimeterx"),
    ("_abck",               "akamai"),
    ("bm_sz",               "akamai"),
    ("ak_bmsc",             "akamai"),
    ("reese84",             "imperva"),
    ("incapsula",           "incapsula"),
    ("x-iinfo",             "imperva"),
    ("datadome",            "datadome"),
    ("dd-b3-traceid",       "datadome"),
    ("cf-turnstile",        "turnstile"),
    ("challenge-platform",  "cloudflare_challenge"),
    ("just a moment",       "cloudflare_challenge"),
    ("cloudflare_managed",  "cloudflare_managed"),
    # Standard captchas
    ("hcaptcha.com",        "hcaptcha"),
    ("h-captcha",           "hcaptcha"),
    ("hcaptcha-enterprise", "hcaptcha_enterprise"),
    ("g-recaptcha",         "recaptcha_v2"),
    ("recaptcha/api2",      "recaptcha_v2"),
    ("recaptcha/enterprise","recaptcha_enterprise"),
    ("recaptcha",           "recaptcha_v2"),
    ("funcaptcha",          "funcaptcha"),
    ("arkoselabs",          "funcaptcha"),
    ("geetest_v4",          "geetest_v4"),
    ("geetest",             "geetest"),
    ("mtcaptcha",           "mtcaptcha"),
    ("keycaptcha",          "keycaptcha"),
    ("botdetect",           "botdetect"),
    ("yandex",              "yandex"),
    ("smartcaptcha",        "yandex"),
    ("you are not a robot", "yandex"),
    ("i'm not a robot",     "yandex"),
    ("turnstile",           "turnstile"),
    # PoW / token-based
    ("altcha",              "altcha"),
    ("friendly-challenge",  "friendly_captcha"),
    ("anubis",              "anubis"),
    ("tendi",               "tendi"),
    # Slider / puzzle
    ("puzzle_slide",        "puzzle_slide"),
    ("puzzle_distance",     "puzzle_distance"),
    ("rotate-captcha",      "rotate"),
    ("captcha-rotate",      "rotate"),
    ("id=\"rotate\"",       "rotate"),
    # AWS
    ("aws-waf-token",       "aws_waf"),
    ("awswaf",              "aws_waf"),
    # Baidu
    ("baiduyun",            "baidu"),
    ("baidu_captcha",       "baidu"),
    # ArcGIS
    ("arcgis",              "arcgis"),
    # Amazon
    ("amazon.com/errors/validateCaptcha", "amazon"),
    ("amzn",                "amazon"),
    # Google — ONLY the real /sorry/ rate-limit page. The yvlrue/sca_esv/emsg
    # markers were removed: they are part of Google's normal SERP/shell
    # template and falsely triggered captcha solving on every Google fetch.
    ("google.com/sorry",    "recaptcha_v2"),
    ("/sorry/",             "recaptcha_v2"),
    ("unusual traffic",     "recaptcha_v2"),
    ("our systems have detected unusual traffic", "recaptcha_v2"),
]

# Response header patterns → captcha type
_HEADER_PATTERNS: list[tuple[str, str]] = [
    ("x-kpsdk-ct",          "kasada"),
    ("x-px-vid",            "perimeterx"),
    ("x-datadome",          "datadome"),
    ("x-iinfo",             "imperva"),
    ("cf-ray",              "cloudflare_challenge"),
    ("x-amzn-waf-action",   "aws_waf"),
    ("x-akamai-request-id", "akamai"),
]

# Types that need a sitekey for direct API solving
_SITEKEY_TYPES = {"recaptcha_v2", "recaptcha_v3", "recaptcha_enterprise",
                  "hcaptcha", "hcaptcha_enterprise", "turnstile", "funcaptcha"}

# Types that need headed browser (ai-captcha-rs) for best results
_HEADED_TYPES = {"geetest", "geetest_v3", "geetest_v4", "puzzle_slide",
                 "puzzle_distance", "rotate", "keycaptcha", "datadome",
                 "aws_waf", "image_select", "coordinates"}


def detect_captcha_type(
    html: str,
    response_headers: Optional[dict] = None,
) -> str:
    """
    Detect captcha type from HTML content and response headers.
    Returns one of the 42 types or 'auto' if unknown.
    """
    html_lower = (html or "").lower()[:50000]

    # Check headers first (most reliable for enterprise anti-bot)
    if response_headers:
        headers_lower = {k.lower(): v.lower() for k, v in response_headers.items()}
        for header_key, ctype in _HEADER_PATTERNS:
            if header_key in headers_lower:
                logger.debug(f"Captcha detected via header '{header_key}': {ctype}")
                return ctype

    # HTML pattern matching
    for pattern, ctype in _HTML_PATTERNS:
        if pattern in html_lower:
            logger.debug(f"Captcha detected via HTML pattern '{pattern}': {ctype}")
            return ctype

    return "auto"


async def extract_sitekey(html: str) -> Optional[str]:
    """Extract captcha sitekey from HTML."""
    patterns = [
        r'data-sitekey\s*=\s*["\']([^"\']+)["\']',
        r'sitekey\s*=\s*["\']([^"\']+)["\']',
        r'data-sitekey["\']?\s*:\s*["\']([^"\']+)["\']',
        r'\\"sitekey\\"\s*:\s*\\"([^\\"]+)\\"',
        r'"sitekey"\s*:\s*"([^"]+)"',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


# ── API calls ─────────────────────────────────────────────────────────────────

async def solve_via_api(
    captcha_type: str,
    site_url: str = "",
    sitekey: str = "",
    image_base64: Optional[str] = None,
    instruction: Optional[str] = None,
    extra: Optional[dict] = None,
) -> dict:
    """Call ai-captcha-bypass /solve/<type> endpoint."""
    payload: dict = {"captcha_type": captcha_type}
    if site_url:
        payload["site_url"] = site_url
    if sitekey:
        payload["site_key"] = sitekey
    if image_base64:
        payload["image_base64"] = image_base64
    if instruction:
        payload["instruction"] = instruction
    if extra:
        payload.update(extra)

    try:
        async with httpx.AsyncClient(timeout=CAPTCHA_TIMEOUT) as client:
            resp = await client.post(
                f"{CAPTCHA_API_URL}/solve/{captcha_type}", json=payload
            )
            if resp.status_code == 200:
                data = resp.json()
                logger.info(
                    f"Captcha API [{captcha_type}]: solved={data.get('success')} "
                    f"confidence={data.get('confidence', 0):.2f}"
                )
                return data
            logger.warning(f"Captcha API [{captcha_type}] HTTP {resp.status_code}")
            return {"success": False, "error": f"HTTP {resp.status_code}"}
    except httpx.ConnectError:
        logger.warning(f"ai-captcha-bypass not reachable at {CAPTCHA_API_URL}")
        return {"success": False, "error": "captcha API unreachable"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def solve_via_api_auto(
    site_url: str,
    image_base64: str,
    html: str = "",
) -> dict:
    """Call /solve/auto — LLM auto-classifies and solves."""
    payload = {
        "captcha_type": "auto",
        "site_url": site_url,
        "image_base64": image_base64,
    }
    if html:
        payload["html"] = html[:5000]  # context for classifier
    try:
        async with httpx.AsyncClient(timeout=CAPTCHA_TIMEOUT) as client:
            resp = await client.post(f"{CAPTCHA_API_URL}/solve/auto", json=payload)
            if resp.status_code == 200:
                return resp.json()
            return {"success": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def solve_via_vm_agent(site_url: str) -> dict:
    """
    Delegate to ai-captcha-rs headed Chrome VM agent.
    The VM opens a real Chrome browser, navigates, solves interactively.
    Returns solved=True + cookies if successful.
    """
    if not CAPTCHA_VM_URL:
        return {"success": False, "error": "CAPTCHA_VM_URL not configured"}
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(
                f"{CAPTCHA_VM_URL}/solve",
                json={"url": site_url, "mode": "headed"},
            )
            if resp.status_code == 200:
                data = resp.json()
                logger.info(f"VM agent result: {data}")
                return data
            return {"success": False, "error": f"VM HTTP {resp.status_code}"}
    except Exception as e:
        logger.warning(f"VM agent error: {e}")
        return {"success": False, "error": str(e)}


# ── Google reCAPTCHA interactive solving ─────────────────────────────────────

async def _solve_google_recaptcha_playwright(
    url: str,
    playwright_strategy: PlaywrightStrategy,
    timeout: float = 60.0,
) -> dict:
    """Solve Google reCAPTCHA via interactive Playwright."""
    import random
    try:
        from playwright.async_api import TimeoutError as PwTimeout
    except ImportError:
        return {"solved": False, "error": "playwright not available"}

    context = None
    lock = None
    try:
        # get_browser() takes _browser_lock itself and asyncio.Lock is not
        # reentrant, so grab the browser first, then hold the lock for our whole
        # page session — PlaywrightStrategy.fetch does the same to keep
        # concurrent use from crashing the shared Node process with EPIPE.
        browser = await playwright_strategy.get_browser()
        if not browser:
            return {"solved": False, "error": "no browser available"}

        lock = getattr(playwright_strategy, "_browser_lock", None)
        if lock:
            await lock.acquire()

        context = await browser.new_context()
        page = await context.new_page()

        logger.info(f"Captcha bridge: navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        await asyncio.sleep(random.uniform(1.0, 2.0))

        if "/sorry/" in page.url.lower():
            html = await page.content()
            cookies = await context.cookies()
            await context.close()
            return {
                "solved": False,
                "html": html,
                "cookies": {c["name"]: c["value"] for c in cookies},
                "error": "Google /sorry/ rate-limit — need different IP/proxy",
            }

        # Check for reCAPTCHA iframe
        recaptcha_iframe = None
        try:
            recaptcha_iframe = await page.wait_for_selector(
                'iframe[src*="recaptcha/api2/anchor"]', timeout=8000,
            )
        except (Exception,):
            pass

        if not recaptcha_iframe:
            h3_count = await page.evaluate("document.querySelectorAll('h3').length")
            if h3_count > 0:
                html = await page.content()
                cookies = await context.cookies()
                await context.close()
                return {
                    "solved": True,
                    "html": html,
                    "cookies": {c["name"]: c["value"] for c in cookies},
                    "error": None,
                }
            await context.close()
            return {"solved": False, "error": "No reCAPTCHA iframe and no results"}

        # reCAPTCHA found — try checkbox + tile solving
        try:
            await recaptcha_iframe.click()
            await asyncio.sleep(random.uniform(1.0, 2.5))

            challenge_iframe = None
            try:
                challenge_iframe = await page.wait_for_selector(
                    'iframe[src*="recaptcha/api2/bframe"]', timeout=5000,
                )
            except (Exception,):
                pass

            if challenge_iframe:
                frame = await challenge_iframe.content_frame()
                if frame:
                    await asyncio.sleep(1)
                    screenshot = await page.screenshot(full_page=False, type="png")
                    b64 = base64.b64encode(screenshot).decode()

                    instr_res = await solve_via_api(
                        "instruction", site_url=url, image_base64=b64
                    )
                    instruction = instr_res.get("solution", "") if instr_res.get("success") else ""

                    if instruction and instruction != "skip":
                        tiles = await frame.query_selector_all(".rc-imageselect-tile")
                        for tile in tiles:
                            tile_b64 = base64.b64encode(
                                await tile.screenshot(type="png")
                            ).decode()
                            vision_res = await solve_via_api(
                                "image_select", site_url=url,
                                image_base64=tile_b64, instruction=instruction,
                            )
                            if vision_res.get("success") and vision_res.get("solution") == "True":
                                await tile.click()
                                await asyncio.sleep(0.3)

                        verify_btn = await frame.query_selector("#recaptcha-verify-button")
                        if verify_btn:
                            await verify_btn.click()
                            await asyncio.sleep(random.uniform(1.0, 2.0))

            await asyncio.sleep(random.uniform(1.0, 2.0))
        except Exception as e:
            logger.debug(f"reCAPTCHA interaction: {e}")

        html = await page.content()
        cookies = await context.cookies()
        final_url = page.url
        await context.close()

        solved = ("<h3" in html and "/search" in final_url) or (
            len(html) > 2000 and "captcha" not in html.lower()[:5000]
        )
        return {
            "solved": solved,
            "html": html,
            "cookies": {c["name"]: c["value"] for c in cookies},
            "error": None if solved else "captcha interaction incomplete",
        }

    except Exception as e:
        logger.warning(f"Google captcha solve error: {e}")
        return {"solved": False, "html": "", "cookies": {}, "error": str(e)}
    finally:
        # goto/screenshot can raise anywhere above, which used to skip close()
        # and leak the context.
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if lock is not None:
            lock.release()


# ── Main entry point ──────────────────────────────────────────────────────────

async def solve_captcha_from_html(
    url: str,
    html: str,
    playwright_strategy: Optional[PlaywrightStrategy] = None,
    response_headers: Optional[dict] = None,
) -> dict:
    """
    Detect and solve captcha from HTML + optional headers.

    Routing logic:
      - Enterprise anti-bot (Kasada, PerimeterX, Akamai, Imperva): API solve
      - Google /sorry/: interactive Playwright
      - Sitekey types (hCaptcha, reCAPTCHA, Turnstile): direct API
      - Visual/interactive (GeeTest, slider): screenshot → API, or VM agent
      - Unknown: screenshot → /solve/auto
    """
    if not html or len(html) < 200:
        return {"solved": False, "error": "HTML too short"}

    captcha_type = detect_captcha_type(html, response_headers)
    sitekey = await extract_sitekey(html)

    logger.info(
        f"Captcha bridge: type={captcha_type} "
        f"sitekey={sitekey[:16] if sitekey else 'none'} "
        f"url={url[:80]}"
    )

    # ── Enterprise anti-bot (PoW-based, no visible captcha) ──────────────────
    enterprise_types = {
        "kasada", "perimeterx", "human_security", "akamai",
        "imperva", "incapsula", "cloudflare_managed",
    }
    if captcha_type in enterprise_types:
        result = await solve_via_api(captcha_type, site_url=url)
        if result.get("success"):
            return {
                "solved": True,
                "token": result.get("solution"),
                "guidance": result.get("guidance"),
                "html": html,
                "cookies": {},
                "error": None,
            }
        # Fallback to VM agent for enterprise if API guidance available
        logger.info(f"Enterprise [{captcha_type}] guidance-only solve, returning token")
        return {
            "solved": result.get("success", False),
            "token": result.get("solution"),
            "guidance": result.get("guidance"),
            "html": html,
            "cookies": {},
            "error": result.get("error"),
        }

    # ── PoW challenges (no visual captcha) ───────────────────────────────────
    pow_types = {"altcha", "friendly_captcha", "anubis", "tendi"}
    if captcha_type in pow_types:
        result = await solve_via_api(captcha_type, site_url=url, extra={"html": html[:10000]})
        return {
            "solved": result.get("success", False),
            "token": result.get("solution"),
            "html": html,
            "cookies": {},
            "error": result.get("error"),
        }

    # ── Google /sorry/ interactive ────────────────────────────────────────────
    is_google = "google." in url and "/search" in url
    if is_google and captcha_type in ("recaptcha_v2", "auto"):
        if playwright_strategy and hasattr(playwright_strategy, "get_browser"):
            logger.info("Google captcha — launching interactive solving")
            return await _solve_google_recaptcha_playwright(url, playwright_strategy)
        return {"solved": False, "error": "Google captcha needs Playwright"}

    # ── Sitekey-based types (direct API) ─────────────────────────────────────
    if sitekey and captcha_type in _SITEKEY_TYPES:
        result = await solve_via_api(captcha_type, site_url=url, sitekey=sitekey)
        if result.get("success"):
            return {
                "solved": True,
                "token": result.get("solution"),
                "html": html,
                "cookies": {},
                "error": None,
            }

    # ── Visual/interactive types (screenshot + vision API) ───────────────────
    if captcha_type in _HEADED_TYPES or captcha_type == "auto":
        # Try VM agent first if configured (best for interactive captchas)
        if CAPTCHA_VM_URL and captcha_type in _HEADED_TYPES:
            logger.info(f"Routing [{captcha_type}] to VM agent")
            vm_result = await solve_via_vm_agent(url)
            if vm_result.get("solved") or vm_result.get("success"):
                return {
                    "solved": True,
                    "html": vm_result.get("html", html),
                    "cookies": vm_result.get("cookies", {}),
                    "error": None,
                }

        # Screenshot via Playwright → vision API
        if playwright_strategy and hasattr(playwright_strategy, "get_browser"):
            shot_context = None
            shot_lock = None
            try:
                browser = await playwright_strategy.get_browser()
                if browser:
                    shot_lock = getattr(playwright_strategy, "_browser_lock", None)
                    if shot_lock:
                        await shot_lock.acquire()
                    shot_context = await browser.new_context()
                    page = await shot_context.new_page()
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)
                    screenshot = await page.screenshot(full_page=False, type="png")
                    await shot_context.close()
                    shot_context = None
                    if shot_lock:
                        shot_lock.release()
                        shot_lock = None

                    b64 = base64.b64encode(screenshot).decode()

                    if captcha_type != "auto":
                        result = await solve_via_api(
                            captcha_type, site_url=url, image_base64=b64
                        )
                    else:
                        result = await solve_via_api_auto(url, b64, html)

                    if result.get("success"):
                        return {
                            "solved": True,
                            "token": result.get("solution"),
                            "html": html,
                            "cookies": {},
                            "error": None,
                        }
            except Exception as e:
                logger.debug(f"Screenshot captcha solve failed: {e}")
            finally:
                if shot_context is not None:
                    try:
                        await shot_context.close()
                    except Exception:
                        pass
                if shot_lock is not None:
                    shot_lock.release()

    return {
        "solved": False,
        "error": f"Captcha type '{captcha_type}' could not be solved "
                 f"(sitekey={'yes' if sitekey else 'no'})",
    }
