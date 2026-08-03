"""
Anti-Bot Detection — Deterministic rule-based engine.
No AI/LLM decisions. Pure heuristic scoring.

Includes TLS fingerprint awareness (JA3/JA4):
- Detects TLS-specific block pages (Cloudflare, Akamai, DataDome)
- Scores higher when the response indicates TLS fingerprint rejection
- Recorded in the domain tracker so the orchestrator can rotate profiles
"""

import re
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Block indicator keywords (lowercase)
_BLOCK_KEYWORDS = [
    "captcha", "verify you are human", "please verify",
    "please wait for verification",  # Reddit JS PoW challenge
    "cloudflare", "access denied", "blocked",
    "rate limit", "unusual traffic", "automated requests",
    "hcaptcha", "recaptcha", "g-recaptcha",
    "challenge-platform", "turnstile",
    "google.com/sorry", "/sp/captcha",
    "enablejs", "/httpservice/retry/enablejs",
    "bot detection", "security check",
    "please complete the security check",
    "why have i been blocked",
    "ray id", "performance & security by",
]

# TLS/SSL fingerprint-specific block indicators
# These indicate the server positively identified the client as a bot via TLS
_TLS_BLOCK_KEYWORDS = [
    "tls fingerprint", "tls fingerprinting", "ja3",
    "browser integrity", "integrity check failed",
    "tls handshake", "tls version", "tls cipher",
    "client hello", "unexpected tls",
    "ssl handshake failed", "ssl error",
    "akamai ghost", "akamai fingerprint",
    "datadome", "data dome",
    "fingerprint mismatch", "client fingerprint",
    "imperva", "incapsula",
    "blocked by security", "waf blocked",
]

_JS_SHELL_INDICATORS = [
    "noscript", "enable javascript",
    "javascript is required", "please enable javascript",
    "this page requires javascript",
    "window.__INITIAL_STATE__", "window.__NEXT_DATA__",
    "js_challenge",  # Reddit JS PoW hidden input
    "please wait for verification",  # Reddit challenge title
]


@dataclass
class AntibotResult:
    """Result of anti-bot detection analysis."""
    score: int  # 0-100 (0 = clean, 100 = definitely blocked)
    status: str  # "ok" | "suspicious" | "blocked"
    action: str  # "accept" | "retry" | "escalate"
    reasons: list[str]


def detect_antibot(
    html: str,
    status_code: int = 200,
    final_url: str = "",
    redirect_count: int = 0,
    content_length: Optional[int] = None,
    response_headers: Optional[dict[str, str]] = None,
) -> AntibotResult:
    """
    Analyze a response for anti-bot protection indicators.
    Includes TLS fingerprint (JA3/JA4/Signals) awareness.

    Returns a deterministic score with recommended action.
    """
    score = 0
    reasons: list[str] = []

    if not html:
        return AntibotResult(
            score=100, status="blocked",
            action="escalate", reasons=["empty_response"]
        )

    html_lower = html.lower()[:20000]  # Only scan first 20KB
    length = content_length or len(html)

    # --- Rule 1: HTTP status code ---
    if status_code in (403, 429, 503):
        score += 40
        reasons.append(f"block_status_{status_code}")
    elif status_code >= 400:
        score += 20
        reasons.append(f"error_status_{status_code}")

    # --- Rule 2: Block keywords ---
    keyword_hits = 0
    for keyword in _BLOCK_KEYWORDS:
        if keyword in html_lower:
            keyword_hits += 1
    if keyword_hits >= 3:
        score += 35
        reasons.append(f"block_keywords_{keyword_hits}")
    elif keyword_hits >= 1:
        score += 15
        reasons.append(f"block_keywords_{keyword_hits}")

    # --- Rule 3: TLS fingerprint block indicators ---
    # If we see TLS-specific block keywords, it means the server
    # flagged the connection at the TLS level — high confidence block
    tls_hits = sum(1 for kw in _TLS_BLOCK_KEYWORDS if kw in html_lower)
    if tls_hits >= 2:
        score += 45
        reasons.append(f"tls_fingerprint_block_{tls_hits}")
    elif tls_hits == 1:
        score += 20
        reasons.append("tls_fingerprint_suspicious")

    # --- Rule 3b: HTTP response headers with TLS/CDN signals ---
    if response_headers:
        headers_lower = {k.lower(): v for k, v in response_headers.items()}

        # Akamai-specific: Ghost identifier header means Akamai WAF is checking
        if "x-akamai-request-id" in headers_lower or "x-akamai" in headers_lower.get("server", ""):
            score += 15
            reasons.append("akamai_waf_detected")

        # Cloudflare-specific headers + block status
        if "cf-ray" in headers_lower:
            if status_code in (403, 429, 503):
                score += 30  # Cloudflare actively blocking
                reasons.append("cloudflare_block")
            elif status_code >= 400:
                score += 10
                reasons.append("cloudflare_proxied")

        # DataDome: custom header when blocking
        if "x-datadome" in headers_lower:
            score += 25
            reasons.append("datadome_detected")

        # Imperva/Incapsula
        if "x-iinfo" in headers_lower or "x-cdn" in headers_lower.get("server", ""):
            if "incapsula" in headers_lower.get("server", "") or status_code in (403, 509):
                score += 20
                reasons.append("imperva_block")

    # --- Rule 4: JS shell / noscript detection ---
    js_shell_hits = sum(1 for ind in _JS_SHELL_INDICATORS if ind in html_lower)
    if js_shell_hits >= 2:
        score += 30
        reasons.append(f"js_shell_{js_shell_hits}")
    elif js_shell_hits == 1:
        score += 10
        reasons.append("js_shell_partial")

    # --- Rule 5: Script ratio ---
    script_tags = len(re.findall(r"<script[\s>]", html_lower))
    total_tags = len(re.findall(r"<[a-z]", html_lower))
    if total_tags > 0:
        script_ratio = script_tags / total_tags
        if script_ratio > 0.4:
            score += 25
            reasons.append(f"high_script_ratio_{script_ratio:.2f}")
        elif script_ratio > 0.25:
            score += 10
            reasons.append(f"moderate_script_ratio_{script_ratio:.2f}")

    # --- Rule 6: Content length ---
    if length < 500:
        score += 20
        reasons.append(f"low_content_{length}")
    elif length < 1000:
        score += 5
        reasons.append(f"short_content_{length}")

    # --- Rule 7: Redirect chain ---
    if redirect_count > 3:
        score += 15
        reasons.append(f"redirect_chain_{redirect_count}")
    elif redirect_count > 1:
        score += 5
        reasons.append(f"redirects_{redirect_count}")

    # --- Rule 8: URL-based block indicators ---
    url_lower = final_url.lower()
    if "/sorry/" in url_lower or "/captcha" in url_lower:
        score += 30
        reasons.append("block_url")
    elif "challenge" in url_lower or "verify" in url_lower:
        score += 15
        reasons.append("challenge_url")

    # --- Rule 9: Google search-specific JS shell detection ---
    # Handles both raw Google HTML and wrapped (FlareSolverr/Playwright) HTML.
    # Wrapped HTML has extra <html>, <head>, <body> layers but still contains <h3>.
    # Only flag as blocked if there's genuinely no <h3> AND there's a captcha/challenge.
    if (
        ("google." in url_lower or "google." in (final_url or "").lower())
        and "/search" in url_lower
    ):
        has_h3 = "<h3" in html_lower or "</h3>" in html_lower
        has_captcha = "/sorry/" in url_lower or "google.com/sorry" in url_lower or "captcha" in html_lower[:10000]
        has_challenge = "challenge-platform" in html_lower or "please verify" in html_lower[:10000]
        
        if not has_h3 and (has_captcha or has_challenge):
            score += 60
            reasons.append("google_captcha_or_challenge")
        elif not has_h3 and len(html) < 5000:
            score += 50
            reasons.append("google_no_results_empty")
        elif not has_h3 and len(html) < 20000:
            score += 25
            reasons.append("google_no_results_partial")

    # Clamp score
    score = min(100, score)

    # Determine status and action
    if score < 40:
        status, action = "ok", "accept"
    elif score < 70:
        status, action = "suspicious", "retry"
    else:
        status, action = "blocked", "escalate"

    result = AntibotResult(
        score=score, status=status,
        action=action, reasons=reasons,
    )

    if score > 0:
        logger.debug(
            f"antibot: score={score} status={status} action={action} "
            f"reasons={reasons}"
        )

    return result


def decide_next_step(
    score: int,
    attempt: int,
    current_method: str,
    strategy_order: list[str],
    max_retries_per_layer: int = 2,
) -> dict:
    """
    Deterministic escalation decision. No AI.

    Returns:
        {
            "action": "accept" | "retry" | "escalate" | "fail",
            "next_method": str | None,
            "retry_count": int
        }
    """
    if score < 40:
        return {"action": "accept", "next_method": None, "retry_count": attempt}

    if score < 70 and attempt < max_retries_per_layer:
        return {
            "action": "retry",
            "next_method": current_method,
            "retry_count": attempt + 1,
        }

    # Escalate to next method
    try:
        current_idx = strategy_order.index(current_method)
    except ValueError:
        current_idx = -1

    next_idx = current_idx + 1
    if next_idx < len(strategy_order):
        return {
            "action": "escalate",
            "next_method": strategy_order[next_idx],
            "retry_count": 0,
        }

    return {"action": "fail", "next_method": None, "retry_count": attempt}
