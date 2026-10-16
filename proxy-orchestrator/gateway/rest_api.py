"""
REST API — FastAPI endpoints for AI agents and monitoring.
"""

import base64
import logging
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from Lib_plus_plus.processors.dom_to_markdown import html_to_markdown
from engine.decision_engine import DecisionEngine
from engine.dom_cleaner import clean_spa
from strategies.base import FetchRequest, header_value, is_binary_response
from services.metrics import metrics

logger = logging.getLogger(__name__)


class FetchRequestBody(BaseModel):
    """Request body for POST /fetch."""
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: Optional[str] = None
    body_b64: Optional[str] = None  # binary-safe HTTP body (base64) for uploads
    params: Optional[dict[str, str]] = None
    timeout: float = 30.0
    proxy_url: Optional[str] = None
    force_strategy: Optional[str] = None
    bypass_cache: bool = False
    session_id: Optional[str] = None
    force_new_session: bool = False
    # Agent-friendly knobs:
    #   use_browser=true  → render with a real (GUI/headless) browser first
    #   browser="gui_chrome|playwright|puppeteer|..." → force that exact strategy
    use_browser: bool = False
    browser: Optional[str] = None
    # Optional CSS selector the browser strategies wait for before returning
    # the page (JS-rendered results after an upload/POST).
    wait_selector: Optional[str] = None
    # raw=true: always return the response bytes in body_b64 (also for text).
    # Binary responses (images, PDFs, files) are returned as bytes regardless.
    raw: bool = False
    # Upload the multipart file in body_b64 by submitting a real form inside
    # the GUI browser (needs a browser strategy). upload_landing: page opened
    # first; result_url_contains: URL fragment that marks the results page.
    in_page_upload: bool = False
    upload_landing: Optional[str] = None
    result_url_contains: Optional[str] = None


class FetchResponseBody(BaseModel):
    """Response body for POST /fetch."""
    success: bool
    status_code: int
    url: str
    final_url: str
    headers: dict[str, str]
    html: str
    markdown: str = ""
    markdown_metadata: dict = Field(default_factory=dict)
    strategy_used: str
    latency: float
    retries: int
    cached: bool
    antibot_score: int = 0
    quality_score: int = 0
    error: Optional[str] = None
    metadata: dict = Field(default_factory=dict)
    # Raw payload for images / PDFs / files (and for raw=true requests).
    content_type: str = ""
    body_b64: Optional[str] = None
    size: int = 0


def _pdf_text(data: bytes, max_pages: int = 50) -> str:
    """Plain text of a PDF (best-effort; empty when pypdf is missing)."""
    try:
        import io as _io
        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        reader = PdfReader(_io.BytesIO(data))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages[:max_pages]).strip()
    except Exception:
        return ""


class ConfigUpdate(BaseModel):
    """Runtime configuration updates."""
    strategy_order: Optional[list[str]] = None
    per_domain_rate_limit: Optional[int] = None
    global_rate_limit: Optional[int] = None


# RIL endpoints helper
_RIL_ENABLED = False

def _get_ril() -> Optional[Any]:
    """Lazy import RIL singleton."""
    global _RIL_ENABLED
    try:
        import sys, os
        _ril_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.pardir)
        sys.path.insert(0, os.path.abspath(_ril_path))
        from ril import ril as _ril_inst
        _RIL_ENABLED = True
        return _ril_inst
    except Exception:
        return None


RETRIEVAL_INTELLIGENCE_LAYER = _get_ril()


def create_rest_app(engine: DecisionEngine) -> FastAPI:
    """Create and configure the FastAPI application."""

    app = FastAPI(
        title="Proxify API",
        description="Proxify — Universal Anti-Bot Bypass Gateway. REST API for AI agents and monitoring",
        version="1.0.0",
    )

    @app.get("/health", tags=["monitoring"])
    async def health():
        """Health check endpoint."""
        from services.metrics import metrics
        # Access private attributes safely — L1 cache may not have a .stats dict
        l1_size = 0
        try:
            if hasattr(engine._l1_cache, "_cache"):
                l1_size = len(engine._l1_cache._cache)
            elif hasattr(engine._l1_cache, "stats"):
                l1_size = engine._l1_cache.stats.get("size", 0)
        except Exception:
            pass
        return {
            "status": "ok",
            "version": "1.0.0",
            "services": {
                "strategies": list(engine._strategies.keys()) if hasattr(engine, "_strategies") else [],
                "cache": {"l1_size": l1_size},
                "sessions": {"active": engine._session_manager.active_count if hasattr(engine._session_manager, "active_count") else 0},
            },
        }

    @app.get("/engine/stats", tags=["monitoring"])
    async def engine_stats():
        """Get engine statistics (delegates to DecisionEngine)."""
        return await engine.get_stats()

    @app.get("/metrics", tags=["monitoring"])
    async def prometheus_metrics():
        """Prometheus-format metrics."""
        return Response(
            content=metrics.generate(),
            media_type=metrics.content_type(),
        )

    @app.get("/stats", tags=["monitoring"])
    async def domain_stats(domain: Optional[str] = Query(None)):
        """Get domain statistics. If domain is specified, returns stats for that domain only."""
        if domain:
            stats = await engine._domain_tracker.get_stats(domain)
            if not stats:
                raise HTTPException(404, f"No stats for domain: {domain}")
            return {domain: stats}
        return await engine._domain_tracker.get_all_stats()

    @app.get("/stats/cache", tags=["monitoring"])
    async def cache_stats():
        """Get cache statistics."""
        return {
            "l1": engine._l1_cache.stats,
            "rate_limiter": await engine._rate_limiter.get_stats(),
        }

    @app.get("/stats/proxies", tags=["monitoring"])
    async def proxy_stats():
        """Get upstream proxy statistics."""
        return await engine._proxy_manager.get_stats()

    @app.get("/stats/circuits", tags=["monitoring"])
    async def circuit_stats():
        """Get circuit breaker states."""
        return await engine._circuit_breaker.get_all_states()

    @app.post("/fetch", tags=["fetch"], response_model=FetchResponseBody)
    async def fetch(body: FetchRequestBody):
        """
        Fetch a URL through the orchestrator.

        The orchestrator will:
        1. Check cache (L1 → L2)
        2. Rate limit the request
        3. Deduplicate concurrent identical requests
        4. Select the optimal strategy based on domain history
        5. Execute with automatic fallback escalation
        6. Cache the result
        7. Return full response with metadata
        """
        # Agent-friendly browser knob: use_browser=true picks a real browser
        # (GUI Chrome VM first — the persistent headful one — falling back to
        # playwright/puppeteer). browser="x" forces an exact strategy.
        force_strategy = body.force_strategy
        if force_strategy is None and body.browser:
            force_strategy = body.browser
        if force_strategy is None and body.use_browser:
            registered = getattr(engine, "_strategies", {})
            for candidate in ("gui_chrome", "playwright", "puppeteer", "nodriver"):
                if candidate in registered:
                    force_strategy = candidate
                    break

        request = FetchRequest(
            url=body.url,
            method=body.method,
            headers=body.headers,
            body=base64.b64decode(body.body_b64) if body.body_b64 else (body.body.encode() if body.body else None),
            params=body.params,
            timeout=body.timeout,
            proxy_url=body.proxy_url,
            force_strategy=force_strategy,
            bypass_cache=body.bypass_cache,
            session_id=body.session_id,
            force_new_session=body.force_new_session,
            wait_selector=body.wait_selector,
            metadata={
                k: v for k, v in {
                    "in_page_upload": body.in_page_upload,
                    "upload_landing": body.upload_landing,
                    "result_url_contains": body.result_url_contains,
                    "keep_raw_html": body.raw or body.in_page_upload,
                }.items() if v
            },
        )

        result = await engine.fetch(request)

        content_type = header_value(result.headers, "content-type")
        binary = bool(result.metadata.get("binary")) or (
            bool(result.body) and is_binary_response(result.headers, result.body)
        )
        payload = result.body or (result.html.encode() if body.raw and result.html else b"")
        body_b64 = base64.b64encode(payload).decode("ascii") if (binary or body.raw) and payload else None

        if binary:
            # no HTML to convert; PDFs get their text as markdown
            is_pdf = "pdf" in content_type.lower() or (result.body or b"")[:4] == b"%PDF"
            text = _pdf_text(result.body) if is_pdf else ""
            markdown_result = {
                "markdown": text,
                "word_count": len(text.split()),
                "extraction_method": "pdf_text" if text else "binary",
                "title": "",
                "success": bool(text) or not is_pdf,
            }
        else:
            # Extract markdown from HTML
            markdown_result = html_to_markdown(
                html=result.html,
                url=result.url,
                strategy=result.strategy_used,
            )

        # `success` must mean "here is the page". A strategy that connected but
        # captured nothing (an empty DOM, a challenge page whose content was
        # stripped) came back success=True with no html and no body, which a
        # caller cannot tell from a real page without inspecting every field.
        # HEAD, 204 and 304 are legitimately empty.
        content_bytes = len(result.body) if result.body else len((result.html or "").encode())
        legitimately_empty = body.method.upper() == "HEAD" or result.status_code in (204, 304)
        success = result.success
        error = result.error
        if success and content_bytes == 0 and not legitimately_empty:
            success = False
            error = error or f"empty content from {result.strategy_used or 'strategy'} (HTTP {result.status_code})"

        return FetchResponseBody(
            success=success,
            status_code=result.status_code,
            url=result.url,
            final_url=result.final_url,
            headers=result.headers,
            html=result.html,
            markdown=markdown_result["markdown"],
            markdown_metadata={
                "word_count": markdown_result["word_count"],
                "extraction_method": markdown_result["extraction_method"],
                "title": markdown_result["title"],
                "success": markdown_result["success"],
            },
            strategy_used=result.strategy_used,
            latency=round(result.latency, 4),
            retries=result.retries,
            cached=result.cached,
            antibot_score=result.antibot_score,
            quality_score=result.quality_score,
            error=error,
            metadata=result.metadata,
            content_type=content_type,
            body_b64=body_b64,
            # Bytes of content actually returned. It used to count body_b64
            # only, so every ordinary HTML fetch reported size 0 even with a
            # full page in `html`, and "size: 0" read as "empty".
            size=len(payload) if body_b64 else content_bytes,
        )

    # =========================================================================
    # RIL (Retrieval Intelligence Layer) — the 8 brains
    # =========================================================================

    # ── Interactive browser bridge ────────────────────────────────────────────
    # These endpoints expose the persistent GUI Chrome page to an MCP client.
    # The page lives in the VM's persistent default context, so an agent
    # inherits the real profile's cookies and Cloudflare clearance tokens
    # rather than starting from a blank session.
    #
    # Strategy is always gui_chrome by design: this is an *interaction* API, and
    # the whole point is the profiled, persona-consistent session.

    def _gui_chrome():
        reg = getattr(engine, "_strategies", {})
        for name, strat in reg.items():
            if getattr(strat, "name", "") == "gui_chrome":
                return strat
        return None

    _CHALLENGE_MARKERS = (
        "Just a moment", "Checking your browser", "Attention Required",
        "challenge-platform", "cf_chl_opt", "Enable JavaScript and cookies",
    )

    async def _detect_challenge(page) -> dict:
        """Decide whether Cloudflare gated this page.

        The decisive signal is the absence of ``cf_clearance``: Cloudflare's
        script tags ('challenge-platform') appear on served pages too, so string
        matching alone produces false positives. A page carrying a clearance
        cookie is real, whatever the markup says.
        """
        try:
            html = (await page.content())[:60000]
        except Exception:
            return {"detected": False, "reason": "content unavailable"}
        try:
            cookies = await page.context.cookies()
            names = {c.get("name") for c in cookies}
        except Exception:
            names = set()

        if "cf_clearance" in names:
            return {"detected": False, "reason": "cf_clearance present"}

        hits = [m for m in _CHALLENGE_MARKERS if m in html]
        if hits:
            return {"detected": True, "markers": hits, "cf_clearance": False}
        return {"detected": False, "reason": "no challenge markers"}

    @app.post("/browser/open", tags=["browser"])
    async def browser_open(body: dict):
        strat = _gui_chrome()
        if strat is None or not getattr(strat, "interactive_available", False):
            return JSONResponse(
                {"ok": False,
                 "error": "gui_chrome VM is not attached; interactive browsing "
                          "needs the persistent GUI Chrome running"},
                status_code=503,
            )
        url = body.get("url")
        if not url:
            return JSONResponse({"ok": False, "error": "url is required"},
                                status_code=400)
        try:
            ix = await strat.interactor()
            page = ix._page  # noqa: SLF001 - bridge by design
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(float(body.get("timeout", 30)) * 1000),
            )
            hydrated = None
            if body.get("wait_for_hydration", True):
                hydrated = await ix.wait_for_hydration(
                    timeout=float(body.get("hydration_timeout", 20))
                )

            # Cloudflare's managed challenge is NOT solvable by the GUI Chrome
            # VM: it answers 200 with a challenge page and never issues
            # cf_clearance. FlareSolverr does solve it. Detect that case and tell
            # the caller which strategy will work, instead of handing back a
            # page that looks fine but is a wall.
            challenge = await _detect_challenge(page)
            out = {"ok": True, "url": page.url, "title": await page.title(),
                   "hydrated": hydrated, "challenge": challenge}
            if challenge.get("detected"):
                out["hint"] = (
                    "Cloudflare challenge detected (no cf_clearance). This page "
                    "is interactive but gated. Use fetch_url with "
                    "strategy='flaresolverr' to read the solved content."
                )
            return out
        except Exception as e:
            logger.warning(f"browser_open failed: {e}")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.post("/browser/action", tags=["browser"])
    async def browser_action(body: dict):
        """Run one interaction verb against the persistent page."""
        strat = _gui_chrome()
        if strat is None or not getattr(strat, "interactive_available", False):
            return JSONResponse(
                {"ok": False, "error": "gui_chrome VM is not attached"},
                status_code=503,
            )
        action = body.get("action")
        if not action:
            return JSONResponse({"ok": False, "error": "action is required"},
                                status_code=400)

        args = {k: v for k, v in body.items()
                if k not in ("action",) and v is not None}

        try:
            ix = await strat.interactor()
            if action == "snapshot":
                limit = int(body.get("limit", 120))
                els = await ix.snapshot(limit=limit)
                return {"ok": True, "count": len(els), "elements": [
                    {"i": e.index, "role": e.role, "name": e.name, "tag": e.tag,
                     "disabled": e.disabled, "focused": e.focused} for e in els]}
            if action == "click":
                r = await ix.click(
                    role=body.get("role"), name=body.get("name"),
                    text=body.get("text"), selector=body.get("selector"),
                    x=body.get("x"), y=body.get("y"),
                    button=body.get("button", "left"),
                    click_count=int(body.get("clicks", 1)),
                    nth=int(body.get("nth", 0)),
                )
            elif action == "type":
                r = await ix.type_text(
                    body.get("text", ""), role=body.get("role"),
                    name=body.get("name"), selector=body.get("selector"),
                    delay=int(body.get("delay_ms", 25)),
                    nth=int(body.get("nth", 0)),
                )
            elif action == "press":
                r = await ix.press_key(body.get("key", "Enter"),
                                       hold_ms=int(body.get("hold_ms", 0)))
            elif action == "scroll":
                r = await ix.scroll(delta_y=int(body.get("delta_y", 600)),
                                    delta_x=int(body.get("delta_x", 0)),
                                    selector=body.get("selector"))
            elif action == "hover":
                r = await ix.hover(**args)
            elif action == "drag":
                r = await ix.drag(to={"x": int(body.get("to_x", 0)),
                                      "y": int(body.get("to_y", 0))}, **args)
            elif action == "wait":
                r = await ix.wait(float(body.get("seconds", 1.0)))
            elif action == "wait_for_hydration":
                ok = await ix.wait_for_hydration(
                    timeout=float(body.get("timeout", 20))
                )
                return {"ok": True, "hydrated": ok}
            elif action == "cursor_position":
                r = await ix.cursor_position()
            elif action == "text":
                html = await ix._page.content()  # noqa: SLF001
                doc = clean_spa(html, url=ix._page.url)  # noqa: SLF001
                return {"ok": doc.success, "words": doc.word_count,
                        "method": doc.method, "error": doc.error,
                        "text": doc.text_content[:8000]}
            elif action == "screenshot":
                import base64 as _b64
                data = await ix.screenshot(full_page=bool(body.get("full_page")))
                return {"ok": True, "bytes": len(data),
                        "png_base64": _b64.b64encode(data).decode()}
            else:
                return JSONResponse(
                    {"ok": False, "error": f"unknown action '{action}'"},
                    status_code=400,
                )
            return {"ok": r.ok, **r.to_dict()}
        except Exception as e:
            logger.warning(f"browser_action {action} failed: {e}")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.post("/browser/close", tags=["browser"])
    async def browser_close():
        strat = _gui_chrome()
        if strat is not None:
            await strat.close_interactive()
        return {"ok": True}

    @app.get("/ril/stats", tags=["ril"])
    async def ril_stats():
        """Get comprehensive RIL statistics from all 8 brains."""
        ril = RETRIEVAL_INTELLIGENCE_LAYER
        if not ril:
            raise HTTPException(503, "RIL not available")
        return await ril.get_full_report()

    @app.get("/ril/domains", tags=["ril"])
    async def ril_domains(limit: int = Query(50, le=200)):
        """Get all domains tracked by RIL's Domain Brain."""
        ril = RETRIEVAL_INTELLIGENCE_LAYER
        if not ril:
            raise HTTPException(503, "RIL not available")
        domains = await ril.domain_brain.get_all()
        sorted_domains = sorted(domains, key=lambda d: d.total_requests, reverse=True)[:limit]
        return [
            {
                "domain": d.domain,
                "best_strategy": d.best_strategy,
                "success_rate": round(d.success_rate * 100, 1),
                "avg_latency": d.latency_text,
                "total_requests": d.total_requests,
                "last_verified": d.last_verified,
                "captcha_vendor": d.captcha_vendor,
            }
            for d in sorted_domains
        ]

    @app.get("/ril/failures", tags=["ril"])
    async def ril_failures(limit: int = Query(20, le=100)):
        """Get top-failing domains from RIL's Failure Brain."""
        ril = RETRIEVAL_INTELLIGENCE_LAYER
        if not ril:
            raise HTTPException(503, "RIL not available")
        failures = await ril.failure_brain.get_all_failing_domains()
        return failures[:limit]

    @app.get("/ril/health", tags=["ril"])
    async def ril_health():
        """Check RIL operational status."""
        ril = RETRIEVAL_INTELLIGENCE_LAYER
        if not ril:
            return {"status": "unavailable", "reason": "RIL module not loaded"}
        ready = await ril.ready()
        return {
            "status": "ready" if ready else "degraded",
            "redis_connected": ready,
            "brains": 8,
            "version": "1.0",
        }

    # =========================================================================
    # Real session cookies — import/refresh/status (stealth toolkit)
    # =========================================================================

    @app.get("/cookies/status", tags=["cookies"])
    async def cookies_status():
        """Show imported real-session cookies (domains, counts, source file)."""
        return await engine.cookie_status()

    @app.post("/cookies/refresh", tags=["cookies"])
    async def cookies_refresh():
        """
        Re-import the real session cookie file from disk (COOKIE_FILE env).

        Export fresh cookies from your browser with scripts/brave_cookies.py,
        drop the file at the configured path, then call this — no restart.
        """
        return await engine.refresh_cookies()

    @app.post("/config", tags=["admin"])
    async def update_config(body: ConfigUpdate):
        """Update runtime configuration."""
        updates = {}
        if body.strategy_order:
            engine._strategy_order = body.strategy_order
            updates["strategy_order"] = body.strategy_order
        return {"updated": updates}

    # =========================================================================
    # Captcha bypass endpoints — passthrough to ai-captcha-bypass (42 types)
    # and ai-captcha-rs VM agent (headed Chrome)
    # =========================================================================

    import os as _os
    import httpx as _httpx
    from engine.captcha_solver_bridge import (
        detect_captcha_type, solve_via_api, solve_via_api_auto, solve_via_vm_agent,
        CAPTCHA_API_URL, CAPTCHA_VM_URL,
    )

    class CaptchaSolveRequest(BaseModel):
        url: str = ""
        captcha_type: str = "auto"
        image_base64: Optional[str] = None
        site_key: Optional[str] = None
        instruction: Optional[str] = None
        html: Optional[str] = None
        use_vm: bool = False  # force VM agent (ai-captcha-rs headed Chrome)

    class CaptchaDetectRequest(BaseModel):
        html: str
        headers: dict[str, str] = Field(default_factory=dict)

    @app.get("/captcha/status", tags=["captcha"])
    async def captcha_status():
        """Check connectivity to ai-captcha-bypass and ai-captcha-rs VM agent."""
        results: dict = {}
        try:
            async with _httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{CAPTCHA_API_URL}/health")
                data = r.json()
                results["bypass_api"] = {
                    "url": CAPTCHA_API_URL,
                    "status": "ok",
                    "supported_types": data.get("count", 0),
                    "model": data.get("model"),
                }
        except Exception as e:
            results["bypass_api"] = {"url": CAPTCHA_API_URL, "status": "error", "error": str(e)}

        if CAPTCHA_VM_URL:
            try:
                async with _httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.get(f"{CAPTCHA_VM_URL}/health")
                    results["vm_agent"] = {"url": CAPTCHA_VM_URL, "status": "ok", **r.json()}
            except Exception as e:
                results["vm_agent"] = {"url": CAPTCHA_VM_URL, "status": "error", "error": str(e)}
        else:
            results["vm_agent"] = {"status": "not_configured"}

        return results

    @app.get("/captcha/types", tags=["captcha"])
    async def captcha_types():
        """List all 42 supported captcha types from ai-captcha-bypass."""
        try:
            async with _httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{CAPTCHA_API_URL}/health")
                return r.json()
        except Exception as e:
            raise HTTPException(503, f"ai-captcha-bypass unreachable: {e}")

    @app.post("/captcha/detect", tags=["captcha"])
    async def captcha_detect(body: CaptchaDetectRequest):
        """Detect captcha type from HTML + headers without solving."""
        ctype = detect_captcha_type(body.html, body.headers or {})
        return {"captcha_type": ctype, "detected": ctype != "auto"}

    @app.post("/captcha/solve", tags=["captcha"])
    async def captcha_solve(body: CaptchaSolveRequest):
        """
        Solve a captcha via ai-captcha-bypass (42 types) or VM agent.

        - Set use_vm=true to force ai-captcha-rs headed Chrome browser.
        - captcha_type='auto' → LLM auto-classifies from screenshot/HTML.
        - Provide image_base64 for visual captchas, site_key for token types.
        """
        if body.use_vm:
            if not CAPTCHA_VM_URL:
                raise HTTPException(503, "VM agent (ai-captcha-rs) not configured — set CAPTCHA_VM_URL")
            result = await solve_via_vm_agent(body.url)
            return result

        if body.captcha_type == "auto":
            if not body.image_base64:
                raise HTTPException(400, "image_base64 required for captcha_type='auto'")
            return await solve_via_api_auto(
                body.url, body.image_base64, body.html or ""
            )

        result = await solve_via_api(
            captcha_type=body.captcha_type,
            site_url=body.url,
            sitekey=body.site_key or "",
            image_base64=body.image_base64,
            instruction=body.instruction,
            extra={"html": body.html[:5000]} if body.html else None,
        )
        return result

    @app.post("/captcha/proxy", tags=["captcha"])
    async def captcha_proxy(request: dict):
        """
        Raw proxy to ai-captcha-bypass — pass any payload to /solve/<type>.
        Useful for direct integration without going through the orchestrator pipeline.
        """
        captcha_type = request.pop("captcha_type", "auto")
        try:
            async with _httpx.AsyncClient(timeout=120.0) as client:
                r = await client.post(
                    f"{CAPTCHA_API_URL}/solve/{captcha_type}", json=request
                )
                return r.json()
        except Exception as e:
            raise HTTPException(503, str(e))

    return app
