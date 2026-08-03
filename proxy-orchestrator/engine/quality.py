"""
Response Quality Filter — Deterministic content quality scoring.
No AI/LLM. Pure heuristic analysis.
"""

import re
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class QualityResult:
    """Result of quality analysis."""
    quality_score: int  # 0-100
    usable: bool
    reasons: list[str]


def score_quality(text: str) -> QualityResult:
    """
    Score the quality of extracted text/HTML content.
    Only passes usable content forward.

    Rules:
    - <300 chars → reject
    - too many scripts / no visible text → reject
    - repetitive / duplicate → low quality
    """
    reasons: list[str] = []
    score = 100  # Start perfect, deduct

    if not text:
        return QualityResult(quality_score=0, usable=False, reasons=["empty"])

    text_length = len(text)

    # --- Rule 1: Minimum length ---
    if text_length < 300:
        score -= 60
        reasons.append(f"too_short_{text_length}")
    elif text_length < 1000:
        score -= 20
        reasons.append(f"short_{text_length}")

    # --- Rule 2: Visible text ratio ---
    # Strip all HTML tags to get raw text
    visible = re.sub(r"<[^>]+>", "", text)
    visible = re.sub(r"\s+", " ", visible).strip()
    visible_len = len(visible)

    if text_length > 0:
        visible_ratio = visible_len / text_length
        if visible_ratio < 0.05:
            score -= 40
            reasons.append(f"no_visible_text_{visible_ratio:.3f}")
        elif visible_ratio < 0.15:
            score -= 15
            reasons.append(f"low_visible_text_{visible_ratio:.3f}")

    # --- Rule 3: Script density ---
    script_blocks = re.findall(r"<script[^>]*>.*?</script>", text, re.DOTALL | re.IGNORECASE)
    script_chars = sum(len(s) for s in script_blocks)
    if text_length > 0 and script_chars / text_length > 0.6:
        score -= 30
        reasons.append("script_heavy")

    # --- Rule 4: Repetitive content ---
    if visible_len > 100:
        # Split into sentences and check for duplicates
        sentences = [s.strip() for s in re.split(r"[.!?]\s+", visible) if len(s.strip()) > 20]
        if len(sentences) > 3:
            unique = set(sentences)
            dup_ratio = 1.0 - (len(unique) / len(sentences))
            if dup_ratio > 0.5:
                score -= 25
                reasons.append(f"repetitive_{dup_ratio:.2f}")

    # --- Rule 5: Error page indicators ---
    error_phrases = [
        "page not found", "404 not found", "500 internal server error",
        "service unavailable", "bad gateway", "gateway timeout",
    ]
    text_lower = text.lower()[:5000]
    error_hits = sum(1 for p in error_phrases if p in text_lower)
    if error_hits > 0:
        score -= 20 * error_hits
        reasons.append(f"error_page_{error_hits}")

    # --- Rule 6: Captcha / challenge page indicators ---
    # NOTE: "trouble accessing" / yvlrue / sca_esv / emsg markers are NOT
    # included — they appear on Google's normal SERP/shell template and would
    # reject real search results.
    challenge_phrases = [
        "please click here",
        "verification required",
        "please wait for verification",
        "just a moment",
        "challenge platform",
        "security check",
        "are you a robot",
        # Yandex SmartCaptcha — the page title/body literally says this
        "you are not a robot",
        "i'm not a robot",
        "smartcaptcha",
        "unusual traffic",
        "our systems have detected",
    ]
    challenge_hits = sum(1 for p in challenge_phrases if p in text_lower)
    if challenge_hits > 0:
        score -= 50 * challenge_hits
        reasons.append(f"challenge_page_{challenge_hits}")

    # --- Rule 7: JS shell / SPA unrendered (tiny visible text) ---
    # React/Vue/Nextcloud return a shell like `<div id="root"></div>` with
    # almost no server-rendered text → markdown would be empty. Reject so the
    # pipeline escalates to a JS-capable strategy instead of returning a
    # contentless page as "success".
    if visible_len < 100:
        score -= 80
        reasons.append(f"js_shell_no_content_{visible_len}")

    # Clamp
    score = max(0, min(100, score))
    usable = score >= 40

    if not usable:
        logger.debug(f"quality: rejected score={score} reasons={reasons}")

    return QualityResult(
        quality_score=score,
        usable=usable,
        reasons=reasons,
    )
