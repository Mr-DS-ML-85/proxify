"""
JS Challenge Solver — Detects and solves client-side JS Proof-of-Work challenges.

Architecture:
  Reddit's PoW challenge (seed doubling) can only be solved by a real JS engine.
  The "solver" is to route the URL through Playwright/nodriver which executes
  the JS natively, then return the real content + cookies.

  For captchas, the solver bridge delegates to ai-captcha-bypass (Ollama vision)
  via Playwright screenshot tiling.

Flow:
  1. Detect JS challenge page from HTML patterns
  2. Re-fetch through Playwright strategy (executes JS, handles PoW)
  3. Extract cookies from the solved session
  4. Return real content + session cookies

  For captchas:
  2b. Playwright renders the page, takes screenshots of tiles
  3b. Send tile images to ai-captcha-bypass (vision model)
  4b. Click identified tiles in browser
  5b. Extract captcha token
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx
from config import config

logger = logging.getLogger(__name__)

# ── Challenge Detection ──────────────────────────────────────────────────────

CHALLENGE_PATTERNS: list[re.Pattern] = [
    # Reddit: "Reddit - Please wait for verification"
    re.compile(r'<title>\s*Reddit\s*-\s*Please\s*wait\s*for\s*verification\s*</title>', re.IGNORECASE),
    # Generic: "verification" in title
    re.compile(r'<title>[^<]*verification[^<]*</title>', re.IGNORECASE),
    # Generic: js_challenge hidden input
    re.compile(r'<input[^>]*name\s*=\s*["\']js_challenge["\'][^>]*>', re.IGNORECASE),
    # Cloudflare challenge
    re.compile(r'<title>[^<]*Just\s*a\s*moment[^<]*</title>', re.IGNORECASE),
    re.compile(r'id\s*=\s*["\']cf-challenge-running["\']', re.IGNORECASE),
    # Cloudflare Turnstile
    re.compile(r'cf-turnstile', re.IGNORECASE),
    # Google: real /sorry/ rate-limit page (NOT the yvlrue/sca_esv markers —
    # those are part of Google's NORMAL SERP/shell template and would
    # false-positive on every successful Google fetch)
    re.compile(r'google\.com/sorry', re.IGNORECASE),
    re.compile(r'unusual traffic', re.IGNORECASE),
    re.compile(r'our systems have detected unusual traffic', re.IGNORECASE),
    # Reddit: "blocked by network security" — bot detection page
    re.compile(r'blocked by network security', re.IGNORECASE),
    re.compile(r'network.security', re.IGNORECASE),
]


# A genuine challenge/block page is SMALL (5-50KB of mostly script/boilerplate)
# AND link-sparse (a bare script wrapper with 1-3 links). Real content pages can
# be megabytes and legitimately contain challenge-ish strings — old.reddit.com's
# classic UI embeds the reCAPTCHA login <script> on EVERY page (134KB of real
# post listings contain the literal string 'recaptcha').
_MAX_CHALLENGE_PAGE_SIZE = 200_000
# Challenge pages are bare script wrappers; real pages have real navigation
# links in the first 15KB. Threshold: real pages almost always exceed this.
_MIN_REAL_LINKS = 8


def _is_real_content_page(html: str) -> bool:
    """A page with many real links in the first 15KB is real content, not a
    challenge/captcha page — even if it mentions 'recaptcha' (old.reddit's
    login script embed is the canonical false-positive)."""
    head = html.lower()[:15000]
    return head.count("href=") >= _MIN_REAL_LINKS


# URL/status-style signals that are UNEQUIVOCAL block indicators — checked
# BEFORE the link-density bail so a link-rich /sorry/ or Cloudflare interstitial
# is still caught (same ordering as FetchResult.is_blocked).
_HARD_BLOCK_MARKERS = [
    "google.com/sorry", "/sp/captcha", "/sorry/",
    "challenge-platform", "cf-challenge",
    "blocked by network security",
]


def _has_hard_block_marker(html: str) -> bool:
    low = html.lower()
    return any(m in low for m in _HARD_BLOCK_MARKERS)


def is_js_challenge(html: str) -> bool:
    """Check if HTML is a JS challenge page."""
    if not html or len(html) < 100:
        return False
    if _has_hard_block_marker(html):
        return True
    if len(html) > _MAX_CHALLENGE_PAGE_SIZE:
        return False
    if _is_real_content_page(html):
        return False
    for pattern in CHALLENGE_PATTERNS:
        if pattern.search(html):
            return True
    return False


# ── Captcha Detection ────────────────────────────────────────────────────────

CAPTCHA_PATTERNS = [
    # Standard
    "hcaptcha.com", "recaptcha", "g-recaptcha", "cf-turnstile",
    "geetest", "funcaptcha", "datadome", "mtcaptcha", "yandex",
    "google.com/sorry", "/sorry/",
    # Enterprise anti-bot
    "kpsdk-", "x-kpsdk-ct",            # Kasada
    "_px3", "perimeterx", "px-captcha", # PerimeterX/HUMAN
    "_abck", "bm_sz", "ak_bmsc",        # Akamai
    "reese84", "incapsula",             # Imperva/Incapsula
    "dd-b3-traceid",                    # DataDome
    # Yandex SmartCaptcha ("Please confirm that you are not a robot")
    "smartcaptcha", "you are not a robot", "i'm not a robot",
    # PoW / token
    "altcha", "friendly-challenge", "anubis",
    # Slider / puzzle
    "puzzle_slide", "puzzle_distance",
    # Cloud
    "aws-waf-token", "awswaf",
    # Amazon
    "validateCaptcha",
    # Cloudflare
    "just a moment", "challenge-platform",
    # Google block / rate-limit pages
    # (yvlrue / sca_esv / emsg markers were REMOVED — they appear on normal
    #  Google SERP/shell pages too and caused false captcha detection)
    "unusual traffic",
    "our systems have detected unusual traffic",
    # Reddit: "blocked by network security" — bot detection page
    "blocked by network security",
    "network security",
]


def is_captcha_page(html: str) -> bool:
    """Check if HTML contains a captcha widget that needs solving.

    Size-guarded: pages over _MAX_CHALLENGE_PAGE_SIZE are real content, not a
    captcha page. Without this, real pages that merely *mention* recaptcha
    (e.g. a JS bundle reference on a real Reddit SERP) get misclassified as
    captcha, and the pipeline wastes time trying to 'solve' real content.
    """
    if not html:
        return False
    if _has_hard_block_marker(html):
        return True
    if len(html) > _MAX_CHALLENGE_PAGE_SIZE:
        return False
    if _is_real_content_page(html):
        return False
    html_lower = html.lower()
    for pattern in CAPTCHA_PATTERNS:
        if pattern in html_lower:
            return True
    return False


@dataclass
class ChallengeResult:
    """Result of solving a JS challenge or captcha."""
    solved: bool
    html: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    final_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None


# ── Playwright-based Challenge Solver ────────────────────────────────────────

async def _fetch_through_playwright(url: str) -> ChallengeResult:
    """Fetch a URL through the Playwright strategy (handles JS execution).

    Uses the proxy-orchestrator's own REST API to route through Playwright.
    This is the key insight: Playwright executes JavaScript natively, so
    Reddit's PoW challenge is auto-solved.
    """
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"http://localhost:{config.API_PORT}/fetch",
                # The marker header prevents infinite recursion: the nested
                # /fetch goes through the DecisionEngine pipeline again, which
                # would otherwise re-detect the challenge and call this solver
                # again (each layer adding a 120s timeout) until the server dies.
                headers={"X-Orchestrator-Challenge-Solve": "skip"},
                json={
                    "url": url,
                    "force_strategy": "playwright",
                    "timeout": 90.0,
                    "bypass_cache": True,
                },
            )
            if resp.status_code != 200:
                return ChallengeResult(
                    solved=False,
                    error=f"Fetch API returned {resp.status_code}",
                )

            data = resp.json()
            html = data.get("html", "") or ""
            success = data.get("success", False)
            status_code = data.get("status_code", 0)
            cookies = data.get("cookies", {})

            if success and html and len(html) > 500:
                # Check if we got real content (not another challenge page)
                if not is_js_challenge(html):
                    final_url = data.get("final_url", "")
                    headers = data.get("headers", {})
                    logger.info(
                        f"Playwright solved challenge for {url}: "
                        f"{len(html)} bytes, {len(cookies)} cookies, "
                        f"final_url={final_url}"
                    )
                    return ChallengeResult(
                        solved=True,
                        html=html,
                        cookies=cookies or {},
                        final_url=final_url,
                        headers=headers,
                    )

            return ChallengeResult(
                solved=False,
                html=html,
                cookies=cookies or {},
                final_url=data.get("final_url", ""),
                headers=data.get("headers", {}),
                error=f"Playwright returned challenge/empty (status={status_code}, len={len(html)})",
            )

    except Exception as e:
        logger.error(f"Playwright fetch error for {url}: {e}")
        return ChallengeResult(solved=False, error=str(e))


async def solve_js_challenge(url: str, html: str = "") -> ChallengeResult:
    """Solve a JS challenge by routing through Playwright.

    The key architectural insight: rather than trying to reverse-engineer
    the PoW algorithm (which changes), we use a real browser engine that
    executes the JavaScript natively.

    This approach works for:
      - Reddit's seed-doubling PoW
      - Cloudflare challenge pages
      - Any JS-based verification
    """
    logger.info(f"Solving JS challenge for {url} via Playwright")
    return await _fetch_through_playwright(url)


# Captcha solving is handled by engine/captcha_solver_bridge.py
# which connects Playwright rendering with ai-captcha-bypass (vision model)
