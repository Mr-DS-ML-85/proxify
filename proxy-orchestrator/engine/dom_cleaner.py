"""
DOM Cleaner — Strips JS, styles, and wrappers from HTML; extracts meaningful content.
Provides Google-specific search result extraction with clean markdown/snippet output.

Pipeline:
  1. Decode Chromium/Playwright wrapper HTML (color-scheme, <pre> JSON wrappers)
  2. Strip <script>, <style>, <svg>, <noscript>, <iframe>, <link>, meta
  3. Extract Google search results (h3, div.g, cite, span.aCOpRe) into structured format
  4. Normalize remaining HTML (remove attributes, clean whitespace)
  5. Return clean HTML + extracted text + metadata
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional
from html import unescape as html_unescape
from urllib.parse import unquote as url_unquote

logger = logging.getLogger(__name__)


@dataclass
class CleanedDocument:
    """Result of DOM cleaning and extraction."""
    clean_html: str
    text_content: str
    title: str = ""
    word_count: int = 0
    success: bool = False
    method: str = "none"
    google_results: list[dict] = field(default_factory=list)
    error: Optional[str] = None


# ── Chromium/Playwright wrapper decoders ─────────────────────────────────────


def _unwrap_chromium_json(html: str) -> str:
    """Detect and unwrap Chromium JSON wrappers.

    Playwright/Scrapling sometimes wraps JSON responses in an HTML page:
      <html><head><meta ...></head><body><pre style="...">{"key": "val"}</pre></body></html>
    This function extracts the JSON from the <pre> block.
    """
    if not html.strip().lower().startswith("<html"):
        return html

    has_color_scheme = "color-scheme" in html.lower()
    has_pre_block = "<pre" in html.lower()

    if has_color_scheme and has_pre_block:
        match = re.search(
            r'<pre[^>]*>(.*?)</pre>', html.strip(), re.IGNORECASE | re.DOTALL
        )
        if match:
            inner_text = match.group(1).strip()
            inner_text = html_unescape(inner_text)
            if inner_text and (inner_text.startswith("{") or inner_text.startswith("[")):
                logger.debug(f"Unwrapped Chromium JSON wrapper ({len(inner_text)} chars)")
                return inner_text

    return html


def _strip_unwanted_elements(html: str) -> str:
    """Remove script, style, SVG, noscript, iframe, link, and meta elements."""
    # Order matters: remove outermost containers first
    patterns = [
        (r'<script[^>]*>.*?</script>', re.DOTALL | re.IGNORECASE),
        (r'<style[^>]*>.*?</style>', re.DOTALL | re.IGNORECASE),
        (r'<svg[^>]*>.*?</svg>', re.DOTALL | re.IGNORECASE),
        (r'<noscript[^>]*>.*?</noscript>', re.DOTALL | re.IGNORECASE),
        (r'<iframe[^>]*>.*?</iframe>', re.DOTALL | re.IGNORECASE),
        (r'<link[^>]*>', re.IGNORECASE),
        (r'<meta[^>]*>', re.IGNORECASE),
        (r'<!--.*?-->', re.DOTALL),  # HTML comments
        (r'<template[^>]*>.*?</template>', re.DOTALL | re.IGNORECASE),
    ]
    for pattern, flags in patterns:
        html = re.sub(pattern, '', html, flags=flags)
    return html


def _strip_event_handlers_and_extra_attrs(html: str) -> str:
    """Remove inline event handlers and data-* attributes for cleanliness."""
    # Remove event handlers (onclick, onload, etc.)
    html = re.sub(r'\s+on\w+\s*=\s*"[^"]*"', '', html, flags=re.IGNORECASE)
    html = re.sub(r"\s+on\w+\s*=\s*'[^']*'", '', html, flags=re.IGNORECASE)
    html = re.sub(r'\s+on\w+\s*=\s*[^\s>]+', '', html, flags=re.IGNORECASE)
    # Remove data-* and ng-* attributes
    html = re.sub(r'\s+data-[^=]+\s*=\s*"[^"]*"', '', html)
    html = re.sub(r"\s+data-[^=]+\s*=\s*'[^']*'", '', html)
    html = re.sub(r'\s+ng-[^=]+\s*=\s*"[^"]*"', '', html)
    # Remove aria-* attributes
    html = re.sub(r'\s+aria-[^=]+\s*=\s*"[^"]*"', '', html)
    return html


def _clean_element_attributes(html: str) -> str:
    """Keep only essential attributes (href, src, alt, title, class, id, name, type, value, role)."""
    allowed_attrs = {
        'href', 'src', 'alt', 'title', 'class', 'id', 'name',
        'type', 'value', 'role', 'rel', 'target',
        'width', 'height',  # Keep for img/table layout
    }
    # Remove all attributes not in the allowed set
    def _clean_attrs(match):
        tag = match.group(0)
        # If it's a closing tag, return as-is
        if tag.startswith('</'):
            return tag
        # Extract tag name
        tag_name_match = re.match(r'<(\w+)', tag)
        if not tag_name_match:
            return tag
        # Find all attributes
        attrs = re.findall(r'''\s+([a-zA-Z][\w-]*)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)''', tag)
        cleaned = [tag_name_match.group(0)]
        for attr_name, attr_value in re.findall(
            r'''\s+([a-zA-Z][\w-]*)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)''', tag
        ):
            if attr_name.lower() in allowed_attrs:
                cleaned.append(f' {attr_name}={attr_value}')
        # Close the tag
        if tag.endswith('/>'):
            cleaned.append(' />')
        else:
            cleaned.append('>')
        return ''.join(cleaned)

    return re.sub(r'<[^>]+>', _clean_attrs, html)


# ── Google-specific extraction ───────────────────────────────────────────────


def _class_contains(cls: str) -> str:
    """Build a regex that matches a class attribute containing the given class name.

    Google uses multi-class attributes like class="VwiC3b yXK7lf p4wth r025kc Hdw6tb"
    instead of single class="VwiC3b". Our patterns must match class attributes that
    *contain* the target class name, not require an exact match.
    """
    escaped = re.escape(cls)
    # Match class="...VwiC3b..." or class='...VwiC3b...'
    return rf'class=["\'][^"\']*{escaped}[^"\']*["\']'


def _extract_google_results(html: str) -> list[dict]:
    """Extract Google search result cards from rendered HTML.

    Handles both old-style (div.g) and new-style (div.MjjYud) Google SERP layouts.
    Google now uses multi-class attributes like class="MjjYud yXK7lf..." so we use
    _class_contains() for partial class matching.

    Returns a list of {title, url, snippet, position}.
    """
    results = []
    if not html or "<h3" not in html.lower():
        return results

    # Try multiple Google SERP container selectors
    # Modern Google uses: div.MjjYud (each result card, with extra classes)
    # Older Google uses: div.g (classic result container)
    # We use regex since we're working with raw HTML

    # Pattern 1: Modern Google SERP (div containing MjjYud class)
    mjj_match = _class_contains("MjjYud")
    result_blocks = re.findall(
        rf'<div[^>]*{mjj_match}[^>]*>.*?</div>\s*'
        rf'(?=<div[^>]*{mjj_match}|<div[^>]*class=["\'](?:g|main)["\']|</div>|$)',
        html,
        re.DOTALL | re.IGNORECASE,
    )

    if not result_blocks:
        # Pattern 2: Classic Google SERP (div.g with h3 inside)
        # Use word boundary \bg\b to avoid matching 'g' inside other class names
        result_blocks = re.findall(
            r'<div[^>]*class=["\'][^"\']*\bg\b[^"\']*["\'][^>]*>.*?'
            r'<h3[^>]*>.*?</h3>.*?</div>',
            html,
            re.DOTALL | re.IGNORECASE,
        )

    if not result_blocks:
        # Pattern 3: Find all yuRUbf (link container) blocks
        # Google wraps each result link in div.yuRUbf
        result_blocks = re.findall(
            r'<div[^>]*class=["\'][^"\']*yuRUbf[^"\']*["\'][^>]*>.*?'
            r'<h3[^>]*>.*?</h3>.*?</div>',
            html,
            re.DOTALL | re.IGNORECASE,
        )

    if not result_blocks:
        # Pattern 4: Just find all h3-containing result-like divs
        # (works for any Google variant as last resort)
        result_blocks = re.findall(
            r'<div[^>]*>.*?<h3[^>]*>.*?</h3>.*?</div>',
            html,
            re.DOTALL | re.IGNORECASE,
        )

    for i, block in enumerate(result_blocks):
        result = _extract_single_google_result(block, i + 1)
        if result:
            results.append(result)

    # Deduplicate by URL
    seen_urls = set()
    unique_results = []
    for r in results:
        if r['url'] not in seen_urls:
            seen_urls.add(r['url'])
            unique_results.append(r)

    return unique_results


def _extract_single_google_result(block: str, position: int) -> Optional[dict]:
    """Extract title, URL, and snippet from a single Google result block."""
    # Title: usually inside <h3> tag (class "LC20lb MBeuO DKV0Md" currently)
    title_match = re.search(r'<h3[^>]*>(.*?)</h3>', block, re.DOTALL | re.IGNORECASE)
    if not title_match:
        return None
    title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip()
    title = html_unescape(title)
    if not title:
        return None

    # URL: try multiple patterns
    url = ""
    # Pattern A: <a href="https://..." ping="..."> (direct link)
    url_match = re.search(
        r'<a[^>]*href=["\'](https?://[^"\']+)["\'][^>]*>',
        block, re.IGNORECASE,
    )
    if url_match:
        url = url_match.group(1)
    if not url:
        # Pattern B: <a href="/url?q=https://..."> (Google redirect link)
        url_match = re.search(
            r'<a[^>]*href=["\'](/url\?q=https?://[^"\'&]+)["\'][^>]*>',
            block, re.IGNORECASE,
        )
        if url_match:
            url = url_unquote(url_match.group(1).replace('/url?q=', ''))
    if not url:
        # Pattern C: <cite>https://...</cite> (displayed URL)
        cite_match = re.search(
            r'<cite[^>]*>(https?://[^<]+)</cite>',
            block, re.IGNORECASE,
        )
        if cite_match:
            url = cite_match.group(1).strip()

    # Snippet: Google uses class "VwiC3b" with additional classes appended.
    # Must use partial class matching: class contains "VwiC3b"
    snippet = ""
    snippet_patterns = [
        # Pattern 1: VwiC3b container (current Google, multi-class)
        rf'<div[^>]*{_class_contains("VwiC3b")}[^>]*>(.*?)</div>',
        # Pattern 2: span.st (older Google)
        rf'<span[^>]*{_class_contains("st")}[^>]*>(.*?)</span>',
        # Pattern 3: line-clamp div (Google's CSS clamp for snippets)
        r'<div[^>]*style=["\'][^"\']*-webkit-line-clamp[^"\']*["\'][^>]*>(.*?)</div>',
        # Pattern 4: HGLrXd or B6fmyf container (classic/alternative snippet)
        rf'<div[^>]*{_class_contains("HGLrXd")}[^>]*>(.*?)</div>',
        rf'<div[^>]*{_class_contains("B6fmyf")}[^>]*>(.*?)</div>',
    ]
    for sp in snippet_patterns:
        sm = re.search(sp, block, re.DOTALL | re.IGNORECASE)
        if sm:
            snippet = re.sub(r'<[^>]+>', '', sm.group(1)).strip()
            snippet = html_unescape(snippet)
            # Clean up Google's "..." and extra whitespace
            snippet = re.sub(r'\s*&nbsp;\s*\.\.\.\s*$', '', snippet)
            snippet = re.sub(r'\s+', ' ', snippet).strip()
            if snippet:
                break

    # If no snippet found, try to get the first meaningful text after the h3
    if not snippet:
        h3_end = title_match.end()
        after_h3 = block[h3_end:min(len(block), h3_end+2000)]
        text_only = re.sub(r'<[^>]+>', ' ', after_h3)
        text_only = re.sub(r'\s+', ' ', text_only).strip()[:300]
        # Filter out navigation/tool text
        skip_phrases = ['result', 'about', 'next', 'previous', 'settings', 'sign in', 'search']
        if len(text_only) > 50 and not any(p in text_only.lower()[:30] for p in skip_phrases):
            snippet = text_only[:250]

    return {
        "title": title,
        "url": url,
        "snippet": snippet,
        "position": position,
    }


# ── Main cleaning pipeline ───────────────────────────────────────────────────


def clean_dom(html: str, url: str = "") -> CleanedDocument:
    """Clean HTML DOM by removing JS, styles, wrappers, and extracting content.

    Args:
        html: Raw HTML from strategy fetch
        url: Original URL (for Google-specific extraction)

    Returns:
        CleanedDocument with clean_html, text_content, google_results
    """
    if not html:
        return CleanedDocument(
            clean_html="", text_content="", success=False, error="empty_html",
        )

    start_len = len(html)

    try:
        # Step 1: Unwrap Chromium JSON wrappers
        html = _unwrap_chromium_json(html)

        # Step 2: Strip unwanted elements
        html = _strip_unwanted_elements(html)

        # Step 3: Strip event handlers and data attributes
        html = _strip_event_handlers_and_extra_attrs(html)

        # Step 4: Clean element attributes (keep only essential ones)
        html = _clean_element_attributes(html)

        # Step 5: Normalize whitespace
        html = re.sub(r'>\s+<', '>\n<', html)  # Newlines between tags
        html = re.sub(r'\s{2,}', ' ', html)     # Collapse internal whitespace
        html = re.sub(r'\n{3,}', '\n\n', html)  # Max 2 newlines

        # Step 6: Extract text content (strip all HTML tags)
        text_content = re.sub(r'<[^>]+>', '', html)
        text_content = html_unescape(text_content)
        text_content = re.sub(r'\s+', ' ', text_content).strip()

        # Step 7: Extract Google search results if applicable
        google_results = []
        if "google." in url.lower() and "/search" in url.lower():
            google_results = _extract_google_results(html)

        method = "dom_cleaner"
        if google_results:
            method = "google_serp_extractor"

        cleaned_len = len(html)
        word_count = len(text_content.split())

        logger.debug(
            f"DOM cleaner: {start_len} → {cleaned_len} chars "
            f"({start_len - cleaned_len} removed), "
            f"{word_count} words, "
            f"{len(google_results)} Google results"
        )

        return CleanedDocument(
            clean_html=html,
            text_content=text_content,
            word_count=word_count,
            success=True,
            method=method,
            google_results=google_results,
        )

    except Exception as e:
        logger.warning(f"DOM cleaner error: {e}")
        return CleanedDocument(
            clean_html=html,  # Return original on error
            text_content=re.sub(r'<[^>]+>', '', html)[:500],
            success=False,
            error=str(e),
        )


def extract_google_serp(html: str) -> list[dict]:
    """Convenience function to extract Google SERP results from raw HTML."""
    doc = clean_dom(html, url="https://www.google.com/search?q=dummy")
    return doc.google_results


def make_google_results_markdown(results: list[dict]) -> str:
    """Format Google search results as clean markdown."""
    if not results:
        return ""
    lines = []
    for r in results:
        lines.append(f"### {r['position']}. [{r['title']}]({r['url']})")
        if r['snippet']:
            lines.append(f"   {r['snippet']}")
        lines.append("")
    return "\n".join(lines)
