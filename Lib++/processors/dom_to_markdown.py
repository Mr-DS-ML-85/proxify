"""
DOM → Clean Markdown Processor — HTML to AI-ready Markdown

Apple-like approach:
  — One pipeline for ALL sites (Google, Reddit, Wikipedia, generic)
  — Strategy-aware: uses metadata from the fetch strategy to pick best cleanup
  — Self-healing: if one extraction method fails, falls through to the next
  — AI-optimized: outputs clean markdown ready for LLM consumption

Pipeline:
  1. Raw HTML from any strategy (curl_cffi_plus, nodriver, drissionpage_plus, etc.)
  2. Site-specific pre-processing (unwrap JSON wrappers, strip Chromium artifacts)
  3. DOM sanitization (scripts, styles, tracking, nav, ads, hidden elements)
  4. Content extraction (main content area detection + heuristics)
  5. Markdown conversion (markdownify with smart formatting)
  6. Post-processing (empty line cleanup, whitespace normalization, truncation)
  7. Output: clean markdown + metadata (title, word_count, extraction_confidence)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Comment, Tag, NavigableString
from markdownify import markdownify as md_convert

_CAPTCHA_PATTERNS = [
    "captcha", "recaptcha", "hcaptcha", "turnstile",
    "cloudflare", "challenges.cloudflare.com",
    "sorry.index", "unusual traffic",
    "our systems have detected unusual traffic",
    "please verify you are a human",
    "verify you are human",
    "access denied", "blocked", "rate limited",
    "403 forbidden", "403 error",
]

_BLOCK_PAGE_INDICATORS = [
    # NOTE: "trouble accessing" / yvlrue / sca_esv / emsg are NOT included —
    # they appear on Google's normal SERP/shell template and would mark real
    # search results as blocked.
    "our systems have detected unusual traffic",
    "please verify you are a human",
    "verify you are human",
    "sorry.index",
    "unusual traffic",
    "please wait for verification",
    "please wait a moment",
    "checking your browser",
    "are you a robot",
    "are you human",
    "anti-bot",
    "bot detection",
    "security check",
    "verification required",
    "blocked by network security",
    "network security",
    # Yandex SmartCaptcha
    "smartcaptcha",
    "you are not a robot",
    "i'm not a robot",
]

_BLOCK_PAGE_TITLE_PATTERNS = [
    # "Google Search" deliberately NOT included — it is the NORMAL title of
    # every Google results page ("<query> - Google Search").
    "Reddit - Please wait for verification",
    "Just a moment",
    "Checking your browser",
    "Verify you are human",
    "Attention required",
    "Access denied",
]

_BLOCK_PAGE_COMBOS = [
    # (title_pattern, body_pattern) - both must be present
    ("Reddit - Please wait for verification", "please wait for verification"),
    ("Just a moment", "checking your browser"),
    ("Verify you are human", "verify you are human"),
]


def _detect_block_page(html: str, url: str) -> Optional[str]:
    """Detect if the HTML is a captcha or block page. Returns block type or None.

    Uses precise detection: checks for title+body combos and specific patterns
    to avoid false positives on normal content that contains words like "blocked".
    """
    html_lower = html.lower()
    title_tag = BeautifulSoup(html, "html.parser").find("title")
    title_text = title_tag.get_text(strip=True).lower() if title_tag else ""

    # Check combo patterns (title + body must both match)
    for title_pat, body_pat in _BLOCK_PAGE_COMBOS:
        if title_pat.lower() in title_text and body_pat.lower() in html_lower:
            return body_pat

    # Check title-specific patterns
    for title_pat in _BLOCK_PAGE_TITLE_PATTERNS:
        if title_pat.lower() in title_text:
            return title_pat.lower()

    # Check specific body patterns (only precise ones, no generic words)
    for pattern in _BLOCK_PAGE_INDICATORS:
        if pattern in html_lower:
            return pattern

    return None


def _detect_captcha(html: str) -> bool:
    """Check if HTML contains a captcha challenge."""
    html_lower = html.lower()
    return any(p in html_lower for p in ["recaptcha", "hcaptcha", "turnstile", "captcha"])


def _is_google_block_page(html: str, url: str) -> bool:
    """Check if this is a Google 'trouble accessing' block page."""
    html_lower = html.lower()
    domain = urlparse(url).netloc.lower()
    if "google" not in domain:
        return False
    return any(indicator in html_lower for indicator in _BLOCK_PAGE_INDICATORS)


# =============================================================================
# Site-Specific Cleaners
# =============================================================================

def _clean_google(soup: BeautifulSoup, url: str) -> BeautifulSoup:
    """Clean Google search results page — extract just the organic results."""
    # Remove header, footer, nav
    for selector in ["header", "footer", "nav", "#hdtb", "#top_nav",
                     "#foot", "#footcnt", "[role='navigation']",
                     ".RNNXgb", ".SDkEP", ".sfibbc"]:
        for el in soup.select(selector):
            el.decompose()
    # Remove ads
    for ad_class in [".ads-ad", ".uEierd", ".pla-unit", "[id^='tads']"]:
        for el in soup.select(ad_class):
            el.decompose()
    # Keep only the main search results area
    main = soup.select_one("#main, #rcnt, #search, [role='main']")
    if main:
        soup = BeautifulSoup(str(main), "html.parser")
    return soup


def _clean_reddit(soup: BeautifulSoup, url: str) -> BeautifulSoup:
    """Clean Reddit thread or listing."""
    # Remove header, footer, sidebar
    for selector in ["header", "footer", "nav", "faceplate-tooltip",
                     "shreddit-app"]:
        for el in soup.select(selector):
            el.decompose()
    # Try to find main content
    main = soup.select_one(
        "shreddit-post, article, div[data-testid='post-container'], "
        "main, [role='main'], .Post, .thread"
    )
    if main:
        soup = BeautifulSoup(str(main), "html.parser")
    return soup


def _clean_wikipedia(soup: BeautifulSoup, url: str) -> BeautifulSoup:
    """Clean Wikipedia article — extract just the content."""
    # Remove sidebar, nav, footer, infobox
    for selector in ["#mw-panel", "#p-navigation", "#p-interaction",
                     "#footer", ".mw-footer", ".infobox", ".sidebar",
                     ".toc", "#toc", ".navbox", ".mw-jump-link",
                     ".noprint", ".mw-empty-elt", ".reflist",
                     ".reference", ".references-small"]:
        for el in soup.select(selector):
            el.decompose()
    # Keep the main content area
    main = soup.select_one("#mw-content-text, #bodyContent, .mw-parser-output")
    if main:
        soup = BeautifulSoup(str(main), "html.parser")
    return soup


# =============================================================================
# Generic Cleaners
# =============================================================================

def _remove_unwanted_elements(soup: BeautifulSoup) -> BeautifulSoup:
    """Remove scripts, styles, tracking, nav, ads, hidden elements."""
    # Scripts & styles
    for tag in soup.find_all(["script", "style", "noscript", "iframe",
                               "svg", "canvas", "template"]):
        tag.decompose()

    # Tracking & analytics
    for tag in soup.find_all(attrs={
        "data-analytics": True, "data-tracking": True,
        "data-ga": True, "data-gtm": True, "data-datalayer": True,
    }):
        tag.decompose()

    # Hidden elements
    for tag in soup.find_all(attrs={"hidden": True, "aria-hidden": "true"}):
        tag.decompose()
    for tag in soup.find_all(style=re.compile(r"display:\s*none", re.IGNORECASE)):
        tag.decompose()

    # Navigation, footer, header, aside
    for tag in soup.find_all(["nav", "footer", "header", "aside",
                               "advertisement", "menu"]):
        tag.decompose()

    # Links to CSS
    for tag in soup.find_all("link", rel="stylesheet"):
        tag.decompose()

    # Meta tags
    for tag in soup.find_all("meta"):
        tag.decompose()

    # Comments. BeautifulSoup strips the <!-- --> delimiters, so matching on
    # them never fires — Comment is a NavigableString subclass, test the type.
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    return soup


def _find_main_content(soup: BeautifulSoup) -> BeautifulSoup:
    """Try to find the main content area using heuristics."""
    # Try common content containers
    main_selectors = [
        "article",
        "[role='main']",
        "main",
        "#content",
        "#main-content",
        "#main",
        ".content",
        ".post-content",
        ".article-body",
        ".entry-content",
        ".post-body",
        "#article",
        ".main-content",
        ".story-body",
        ".PageContent",
        "[data-testid='content']",
    ]

    for selector in main_selectors:
        el = soup.select_one(selector)
        if el:
            # Only use if it has meaningful text
            text = el.get_text(strip=True)
            if len(text) > 200:
                return BeautifulSoup(str(el), "html.parser")

    # If no main content found, return the body
    body = soup.find("body")
    if body:
        return BeautifulSoup(str(body), "html.parser")

    return soup


def _clean_whitespace(text: str) -> str:
    """Normalize whitespace: remove excessive blank lines, trim lines."""
    # Remove lines that are just whitespace
    lines = text.split("\n")
    cleaned = []
    blank_count = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            blank_count += 1
            if blank_count <= 2:  # Allow at most 2 consecutive blank lines
                cleaned.append("")
        else:
            blank_count = 0
            cleaned.append(stripped)
    return "\n".join(cleaned)


# =============================================================================
# Main Processor
# =============================================================================

class DomToMarkdownProcessor:
    """
    Unified HTML → Clean Markdown pipeline.
    
    Usage:
        processor = DomToMarkdownProcessor()
        result = processor.process(html="<html>...", url="https://...", 
                                    strategy="nodriver")
        markdown = result["markdown"]
        title = result["title"]
    
    Apple-like:
      — Auto-detects site type (Google, Reddit, Wikipedia, generic)
      — Applies site-specific cleaning for best results
      — Falls back gracefully if any step fails
      — Returns consistent structure regardless of input
    """

    _SITE_HANDLERS = {
        "google.": _clean_google,
        "reddit.com": _clean_reddit,
        "wikipedia.org": _clean_wikipedia,
    }

    def __init__(self, max_length: int = 50000):
        self._max_length = max_length

    def _detect_site(self, url: str) -> Optional[str]:
        """Detect which site-specific handler to use."""
        domain = urlparse(url).netloc.lower()
        for pattern, handler in self._SITE_HANDLERS.items():
            if pattern.endswith("."):
                if pattern.rstrip(".") in domain:
                    return pattern
            else:
                if domain == pattern or domain.endswith("." + pattern):
                    return pattern
        return None

    def process(
        self,
        html: str,
        url: str = "",
        strategy: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Process HTML into clean markdown.
        
        Args:
            html: Raw HTML from any strategy
            url: The source URL (used for site detection)
            strategy: Strategy name (e.g., "nodriver", "curl_cffi_plus")
            metadata: Additional metadata from the fetch
        
        Returns:
            dict with keys: markdown, title, word_count, extraction_method,
                            url, strategy, success
        """
        start_time = __import__("time").time()

        if not html or len(html.strip()) < 50:
            return {
                "markdown": "",
                "title": "",
                "word_count": 0,
                "extraction_method": "none",
                "url": url,
                "strategy": strategy,
                "success": False,
                "error": "Empty or too short HTML",
                "latency": 0,
            }

        metadata = metadata or {}
        result = {
            "url": url,
            "strategy": strategy,
            "success": False,
            "error": "",
            "markdown": "",
            "title": "",
            "word_count": 0,
            "extraction_method": "generic",
            "latency": 0,
        }

        try:
            # Step 1: Parse HTML
            soup = BeautifulSoup(html, "html.parser")
            
            # Step 2: Extract title
            title_tag = soup.find("title")
            result["title"] = title_tag.get_text(strip=True) if title_tag else ""

            # Step 3: Detect captcha/block pages
            block_type = _detect_block_page(html, url)
            if block_type:
                result["extraction_method"] = f"blocked_{block_type[:30]}"
                result["error"] = f"Blocked/captcha page detected: {block_type}"
                result["latency"] = __import__("time").time() - start_time
                return result

            # Step 4: Site-specific cleaning
            site_pattern = self._detect_site(url)
            if site_pattern:
                handler = self._SITE_HANDLERS[site_pattern]
                try:
                    soup = handler(soup, url)
                    result["extraction_method"] = f"site_specific_{site_pattern.replace('.', '_')}"
                except Exception as e:
                    logger.debug(f"Site-specific cleanup failed for {url}: {e}")
                    # Fall through to generic cleaning
                    result["extraction_method"] = "generic_with_fallback"

            # Step 5: Generic DOM sanitization
            soup = _remove_unwanted_elements(soup)

            # Step 5: Find main content area
            soup = _find_main_content(soup)

            # Step 6: Convert to Markdown
            # markdownify options for clean output
            markdown = md_convert(
                str(soup),
                heading_style="ATX",        # Use # style headings
                bullets="-",                 # Use - for bullet lists
                strip=["img", "video", "audio"],  # Remove media
                autolinks=False,             # Don't auto-link bare URLs
                default_title=False,         # Don't add title attributes
            )

            # Step 7: Post-process markdown
            markdown = _clean_whitespace(markdown)

            # Step 8: Truncate if too long
            if len(markdown) > self._max_length:
                markdown = markdown[:self._max_length]
                markdown += "\n\n[...truncated]"

            word_count = len(markdown.split())

            result["markdown"] = markdown
            result["word_count"] = word_count
            result["success"] = word_count > 10
            result["latency"] = __import__("time").time() - start_time

            if not result["success"]:
                result["error"] = f"Too short after extraction ({word_count} words)"

            return result

        except Exception as e:
            logger.warning(f"DomToMarkdown processing error: {e}")
            result["error"] = str(e)
            result["latency"] = __import__("time").time() - start_time
            return result


# =============================================================================
# Convenience function
# =============================================================================

_processor: Optional[DomToMarkdownProcessor] = None


def html_to_markdown(
    html: str,
    url: str = "",
    strategy: str = "",
    max_length: int = 50000,
) -> dict[str, Any]:
    """
    Quick one-shot HTML → Markdown conversion.
    
    Example:
        result = html_to_markdown(html, url="https://google.com/search?q=ai")
        print(result["markdown"])
    """
    global _processor
    if _processor is None:
        _processor = DomToMarkdownProcessor(max_length=max_length)
    return _processor.process(html=html, url=url, strategy=strategy)


# =============================================================================
# Strategy Post-Processor — Wraps any fetch result with markdown extraction
# =============================================================================

def attach_markdown_to_result(
    fetch_result: dict[str, Any],
    url: str = "",
    strategy: str = "",
) -> dict[str, Any]:
    """
    Take a raw fetch result dict and attach clean markdown.
    
    This is designed to be called AFTER any strategy fetch.
    Works with any strategy's FetchResult.
    
    Args:
        fetch_result: The result dict from any strategy's fetch()
        url: The source URL (defaults to fetch_result.get("url"))
        strategy: Strategy name (defaults to fetch_result.get("strategy_used"))
    
    Returns:
        The same fetch_result dict with added 'markdown' key
    """
    html = fetch_result.get("html", "")
    source_url = url or fetch_result.get("url", "")
    source_strategy = strategy or fetch_result.get("strategy_used", "")
    
    md_result = html_to_markdown(
        html=html,
        url=source_url,
        strategy=source_strategy,
    )
    
    fetch_result["markdown"] = md_result["markdown"]
    fetch_result["markdown_metadata"] = {
        "word_count": md_result["word_count"],
        "extraction_method": md_result["extraction_method"],
        "title": md_result["title"],
        "success": md_result["success"],
    }
    
    return fetch_result
