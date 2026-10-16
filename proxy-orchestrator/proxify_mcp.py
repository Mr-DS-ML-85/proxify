#!/usr/bin/env python3
"""
proxify-mcp — browser control for AI agents, over MCP.

Exposes Proxify's interaction layer as MCP tools so an agent can drive a real
browser the way a person does: look at the page's structure, pick a target by
role and accessible name, act on it, and read the result.

Perception is element-first. The agent asks for a snapshot — role, accessible
name, state — which is a few KB of text rather than a screenshot, and it does
not need to guess pixel coordinates. Mouse and keyboard input remain available
for canvas surfaces, native menus, drag, and hover, where no addressable
element exists.

Transport: stdio (default) or streamable-http with --http.

Usage:
    python proxify_mcp.py                       # stdio, for local MCP clients
    python proxify_mcp.py --http --port 8090    # streamable HTTP
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any, Optional

logger = logging.getLogger("proxify-mcp")

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    print(
        "The 'mcp' package is required:  pip install 'mcp>=1.2'\n"
        "Also install a browser:        playwright install chromium",
        file=sys.stderr,
    )
    raise

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.dom_cleaner import clean_spa  # noqa: E402
from engine.interaction import PageInteractor  # noqa: E402

mcp = FastMCP("proxify-mcp")

# ── Session state ─────────────────────────────────────────────────────────────

# One browser and one page for the lifetime of the server, mirroring a person's
# single browser window. State persists across tool calls so an agent can open a
# page, click, type, and screenshot in separate turns.
_STATE: dict[str, Any] = {
    "pw": None,
    "browser": None,
    "context": None,
    "page": None,
    "cdp": None,
}

PROXY_REST = os.getenv("PROXIFY_REST_URL", "http://127.0.0.1:8081")
CHROMIUM = os.getenv("CHROMIUM_PATH") or None
HEADLESS = os.getenv("PROXIFY_MCP_HEADLESS", "1") not in ("0", "false", "False")

# When the orchestrator's GUI Chrome VM is attached, browser tools are delegated
# to it over REST so the agent drives the *persistent profiled* session — real
# cookies, real Cloudflare clearance tokens, coherent persona. That is a
# materially stronger position than a fresh local browser. Falls back to a local
# Playwright browser when the bridge is unavailable.
BRIDGE = os.getenv("PROXIFY_MCP_BRIDGE", "auto")  # auto | local


def _rest(path: str, payload: dict, timeout: float = 90.0) -> dict:
    import urllib.request

    req = urllib.request.Request(
        f"{PROXY_REST}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _bridge_ready() -> bool:
    """True when the orchestrator has an attached GUI Chrome to drive."""
    if BRIDGE == "local":
        return False
    try:
        import urllib.request

        with urllib.request.urlopen(f"{PROXY_REST}/health", timeout=4) as r:
            return json.loads(r.read()).get("status") == "ok"
    except Exception:
        return False


async def _via_bridge(action: str, **kwargs: Any) -> dict:
    return _rest("/browser/action", {"action": action, **kwargs})


def _page() -> Any:
    if _STATE["page"] is None:
        raise RuntimeError(
            "No page open. Call browser_open(url=...) first."
        )
    return _STATE["page"]


def _ix() -> PageInteractor:
    return PageInteractor(_page(), _STATE["cdp"])


async def _ensure_browser() -> None:
    """Start the persistent browser on first use."""
    if _STATE["browser"] is not None:
        return

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    launch: dict[str, Any] = {"headless": HEADLESS}
    if CHROMIUM:
        launch["executable_path"] = CHROMIUM
    browser = await pw.chromium.launch(**launch)

    context = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        timezone_id=os.getenv("PROXIFY_TZ", "Asia/Dhaka"),
    )
    page = await context.new_page()
    try:
        cdp = await context.new_cdp_session(page)
    except Exception:
        cdp = None

    _STATE.update(
        pw=pw, browser=browser, context=context, page=page, cdp=cdp
    )


# ── Navigation ────────────────────────────────────────────────────────────────


@mcp.tool()
async def browser_open(
    url: str,
    wait_for_hydration: bool = True,
    timeout: float = 30.0,
) -> dict:
    """Open a URL in the persistent browser and return its title and URL.

    Use this first. The page persists for later clicks, typing and screenshots.
    Set wait_for_hydration on single-page apps so the agent never reads a
    half-rendered shell.

    When the Proxify GUI Chrome VM is running, this drives that persistent
    profiled session (real cookies and clearance tokens). Otherwise it falls
    back to a local Playwright browser.
    """
    if _bridge_ready():
        res = _rest("/browser/open", {
            "url": url, "wait_for_hydration": wait_for_hydration,
            "timeout": timeout,
        })
        if res.get("ok"):
            return {**res, "driver": "gui_chrome_vm"}
        logger.debug(f"bridge open failed, using local browser: {res.get('error')}")

    await _ensure_browser()
    page = _page()
    await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))

    hydrated = None
    if wait_for_hydration:
        hydrated = await _ix().wait_for_hydration(timeout=min(timeout, 20))

    return {
        "url": page.url,
        "title": await page.title(),
        "hydrated": hydrated,
        "driver": "local",
    }


@mcp.tool()
async def browser_snapshot(limit: int = 120) -> dict:
    """Return the page's interactive elements: role, accessible name, state.

    This is how the agent 'sees' the page — the same query a screen reader
    runs. Far cheaper than a screenshot and stable across layout changes.
    Read this before clicking, so the target is chosen by meaning rather than
    by guessed coordinates.
    """
    if _bridge_ready():
        return await _via_bridge("snapshot", limit=limit)

    elements = await _ix().snapshot(limit=limit)
    return {
        "url": _page().url,
        "count": len(elements),
        "elements": [
            {"i": e.index, "role": e.role, "name": e.name, "tag": e.tag,
             "disabled": e.disabled, "focused": e.focused}
            for e in elements
        ],
    }


@mcp.tool()
async def browser_click(
    role: Optional[str] = None,
    name: Optional[str] = None,
    text: Optional[str] = None,
    selector: Optional[str] = None,
    x: Optional[int] = None,
    y: Optional[int] = None,
    button: str = "left",
    clicks: int = 1,
    nth: int = 0,
) -> dict:
    """Click a target.

    Prefer role+name (e.g. role='button', name='Submit'). Falls back to visible
    text, then a CSS selector, then raw coordinates. Clicks are dispatched as a
    real press/release sequence, not a single synthetic event.

    clicks=2 for double-click, 3 for triple-click. nth selects the 0-based
    match when several elements share a name.
    """
    if _bridge_ready():
        return await _via_bridge(
            "click", role=role, name=name, text=text, selector=selector,
            x=x, y=y, button=button, clicks=clicks, nth=nth,
        )
    r = await _ix().click(
        role=role, name=name, text=text, selector=selector,
        x=x, y=y, button=button, click_count=clicks, nth=nth,
    )
    return r.to_dict()


@mcp.tool()
async def browser_type(
    text: str,
    role: Optional[str] = None,
    name: Optional[str] = None,
    selector: Optional[str] = None,
    nth: int = 0,
    delay_ms: int = 25,
) -> dict:
    """Focus a field and type text into it, key by key so handlers fire.

    If no field is matched, the text goes to whatever currently has focus.
    """
    if _bridge_ready():
        return await _via_bridge(
            "type", text=text, role=role, name=name, selector=selector,
            nth=nth, delay_ms=delay_ms,
        )
    r = await _ix().type_text(
        text, role=role, name=name, selector=selector,
        delay=delay_ms, nth=nth,
    )
    return r.to_dict()


@mcp.tool()
async def browser_press(
    key: str, hold_ms: int = 0
) -> dict:
    """Press a key or key combo, e.g. 'Enter', 'ctrl+a', 'PageDown'.

    Names are normalised, so 'ctrl' works as well as 'Control'. Set hold_ms to
    hold a modifier down, e.g. hold_ms=500 with key='shift'.
    """
    if _bridge_ready():
        return await _via_bridge("press", key=key, hold_ms=hold_ms)
    r = await _ix().press_key(key, hold_ms=hold_ms)
    return r.to_dict()


@mcp.tool()
async def browser_scroll(
    delta_y: int = 600, delta_x: int = 0, selector: Optional[str] = None
) -> dict:
    """Scroll the page, or a specific scrollable container by selector."""
    if _bridge_ready():
        return await _via_bridge("scroll", delta_y=delta_y, delta_x=delta_x,
                                 selector=selector)
    r = await _ix().scroll(delta_y=delta_y, delta_x=delta_x, selector=selector)
    return r.to_dict()


@mcp.tool()
async def browser_hover(role: Optional[str] = None, name: Optional[str] = None,
                        text: Optional[str] = None,
                        selector: Optional[str] = None) -> dict:
    """Hover an element — needed for menus and tooltips that only appear on hover."""
    if _bridge_ready():
        return await _via_bridge("hover", role=role, name=name, text=text,
                                 selector=selector)
    r = await _ix().hover(role=role, name=name, text=text, selector=selector)
    return r.to_dict()


@mcp.tool()
async def browser_drag(to_x: int, to_y: int, role: Optional[str] = None,
                       name: Optional[str] = None,
                       selector: Optional[str] = None) -> dict:
    """Drag from a matched element (or the viewport) to a coordinate."""
    if _bridge_ready():
        return await _via_bridge("drag", to_x=to_x, to_y=to_y, role=role,
                                 name=name, selector=selector)
    r = await _ix().drag(to={"x": to_x, "y": to_y}, role=role, name=name,
                         selector=selector)
    return r.to_dict()


@mcp.tool()
async def browser_screenshot(
    path: Optional[str] = None, full_page: bool = False
) -> dict:
    """Capture the page as PNG. Returns the saved path and byte size.

    Prefer browser_snapshot for understanding the page; use a screenshot when
    the layout or an image actually matters.
    """
    if _bridge_ready():
        return await _via_bridge("screenshot", full_page=full_page)

    data = await _ix().screenshot(path=path, full_page=full_page)
    size = len(data) if isinstance(data, (bytes, bytearray)) else 0
    return {"path": path, "bytes": size, "full_page": full_page}


@mcp.tool()
async def browser_wait(seconds: float = 1.0) -> dict:
    """Wait a fixed number of seconds — for animations or slow XHRs."""
    if _bridge_ready():
        return await _via_bridge("wait", seconds=seconds)
    r = await _ix().wait(seconds)
    return r.to_dict()


@mcp.tool()
async def browser_wait_for_hydration(timeout: float = 20.0) -> dict:
    """Wait until a client-rendered app has actually mounted its content."""
    if _bridge_ready():
        return await _via_bridge("wait_for_hydration", timeout=timeout)
    ok = await _ix().wait_for_hydration(timeout=timeout)
    return {"hydrated": ok, "url": _page().url}


@mcp.tool()
async def page_text() -> dict:
    """Return the page's readable text, cleaned of navigation and boilerplate.

    Uses the SPA cleaner: isolates the real content region and drops nav,
    aside and footer. Flags unhydrated pages instead of returning a skeleton.
    """
    if _bridge_ready():
        return await _via_bridge("text")

    html = await _page().content()
    doc = clean_spa(html, url=_page().url)
    return {
        "url": _page().url,
        "success": doc.success,
        "method": doc.method,
        "words": doc.word_count,
        "error": doc.error,
        "text": doc.text_content[:8000],
    }


def _rest_get(path: str, timeout: float = 10.0) -> dict:
    """GET a REST endpoint. /health and friends are GET-only routes."""
    import urllib.request

    try:
        with urllib.request.urlopen(f"{PROXY_REST}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool()
async def proxify_strategies() -> dict:
    """List every fetching strategy Proxify can use, with health and rank.

    Call this when a target resists, to pick a strategy deliberately instead of
    guessing. Strategies are ordered cheapest-first by Proxify itself.
    """
    res = _rest_get("/health")
    strategies = res.get("services", {}).get("strategies", [])
    guidance = {
        "curl_cffi_plus": "TLS impersonation. Cheapest; clears most bot checks.",
        "simple": "Plain HTTPS with a realistic header set.",
        "drissionpage_plus": "Hybrid session + Chromium page automation.",
        "nodriver": "CDP-direct, no WebDriver leaks. Good JS-heavy pages.",
        "tls_rotator": "Rotates TLS profile per domain.",
        "scrapling": "Stealthy scraper.",
        "flaresolverr": "Solves Cloudflare's managed challenge. Use for "
                        "Cloudflare-gated targets.",
        "playwright": "Full Chromium automation.",
        "puppeteer": "Node.js automation.",
        "puppeteer_plus": "Inline script + proxy fallback.",
        "gui_chrome": "Persistent headful Chrome VM with real profile. "
                      "Best on Google's consent gate and fingerprint checks, "
                      "but does NOT solve Cloudflare's challenge.",
    }
    return {
        "strategies": strategies,
        "count": len(strategies),
        "guidance": guidance,
        "tip": "Pass strategy='flaresolverr' to fetch_url for Cloudflare targets.",
    }


@mcp.tool()
async def fetch_url(
    url: str,
    as_markdown: bool = True,
    strategy: Optional[str] = None,
    use_browser: bool = False,
    timeout: float = 90.0,
) -> dict:
    """Fetch a URL through Proxify's tiered anti-bot pipeline.

    No interaction needed — this delegates to the orchestrator, which escalates
    through TLS impersonation and headless browsers automatically.

    Pass strategy= to pin an exact one (see proxify_strategies for the list and
    when each is appropriate), or use_browser=true to force a real browser.
    For a Cloudflare-gated URL, use strategy='flaresolverr' — the interactive
    browser cannot clear that challenge.

    Use this for reading pages; use browser_* when the agent must click, type
    or log in.
    """
    payload: dict[str, Any] = {
        "url": url, "method": "GET", "as_markdown": as_markdown,
    }
    if strategy:
        payload["force_strategy"] = strategy
    if use_browser:
        payload["use_browser"] = True

    res = _rest("/fetch", payload, timeout=timeout)
    if not res.get("success") and res.get("antibot_score", 0) >= 60:
        res["hint"] = (
            "Looks blocked. Retry with strategy='flaresolverr' for Cloudflare, "
            "or strategy='gui_chrome' for fingerprint-gated targets."
        )
    return res


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description="proxify-mcp")
    ap.add_argument("--http", action="store_true", help="serve streamable HTTP")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()