"""
Base Strategy — Abstract interface for all fetch strategies.
Defines FetchRequest and FetchResult data models.
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class FetchRequest:
    """Represents a fetch request to be processed by a strategy."""
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: Optional[bytes] = None
    params: Optional[dict[str, str]] = None
    timeout: float = 30.0
    proxy_url: Optional[str] = None
    force_strategy: Optional[str] = None
    bypass_cache: bool = False
    session_id: Optional[str] = None
    force_new_session: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    """Result of a fetch operation."""
    success: bool
    status_code: int = 0
    url: str = ""
    final_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    html: str = ""
    body: bytes = b""
    cookies: dict[str, str] = field(default_factory=dict)
    strategy_used: str = ""
    latency: float = 0.0
    retries: int = 0
    error: Optional[str] = None
    cached: bool = False
    antibot_score: int = 0
    quality_score: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_blocked(self) -> bool:
        """Check if the response indicates the request was blocked."""
        if self.status_code in (403, 429, 503):
            return True
        # Check if the URL itself points to a captcha or a block page
        if "/sp/captcha" in self.final_url or "captcha" in self.final_url.lower() or "/sorry/index" in self.final_url:
            return True
        # Phrase scan only applies to challenge-shaped pages. Real pages (e.g.
        # a 134KB old.reddit listing) legitimately contain the literal strings
        # "recaptcha"/"captcha" (old.reddit embeds the reCAPTCHA login script
        # on every page) — flagging them would discard real content
        # (confirmed on old.reddit.com). Challenge pages are SMALL and
        # link-sparse; real pages have real navigation links in the first 15KB.
        if self.html and len(self.html) > 200_000:
            return False
        head = self.html.lower()[:15000]  # Check first 15KB for blocks
        if head.count("href=") >= 8:
            return False
        # Check for common block indicators in HTML (including Google JS enforcement)
        block_phrases = [
            "captcha", "cloudflare", "blocked", "access denied",
            "rate limit", "bot detection", "please verify",
            "unusual traffic", "automated requests",
            "google.com/sorry", "/sp/captcha", "challenge-platform", 
            "hcaptcha", "recaptcha", "g-recaptcha",
            "enablejs", "noscript", "/httpservice/retry/enablejs"
        ]
        return any(phrase in head for phrase in block_phrases)

    @classmethod
    def from_dict(cls, data: dict) -> 'FetchResult':
        import base64
        body_b64 = data.get("body_b64")
        body = base64.b64decode(body_b64) if body_b64 else b""
        
        return cls(
            success=data.get("success", True),
            status_code=data.get("status_code", 200),
            url=data.get("url", ""),
            final_url=data.get("final_url", ""),
            headers=data.get("headers", {}),
            html=data.get("html", ""),
            body=body,
            cookies=data.get("cookies", {}),
            strategy_used=data.get("strategy_used", "cache"),
            latency=data.get("latency", 0.0),
            error=data.get("error"),
            metadata=data.get("metadata", {}),
        )

    def to_dict(self) -> dict:
        import base64
        """Serialize for caching, safely encoding bytes."""
        return {
            "success": self.success,
            "status_code": self.status_code,
            "url": self.url,
            "final_url": self.final_url,
            "headers": self.headers,
            "html": self.html,
            "body_b64": base64.b64encode(self.body).decode("ascii") if self.body else "",
            "cookies": self.cookies,
            "strategy_used": self.strategy_used,
            "latency": self.latency,
            "error": self.error,
            "metadata": self.metadata,
        }


class BaseStrategy(ABC):
    """Abstract base class for all fetch strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy name identifier."""
        ...

    @property
    def priority(self) -> int:
        """Lower = tried first. Override in subclasses."""
        return 50

    @abstractmethod
    async def fetch(self, request: FetchRequest) -> FetchResult:
        """Execute a fetch request. Must be implemented by subclasses."""
        ...

    async def initialize(self) -> None:
        """Optional initialization (e.g., starting browser instances)."""
        pass

    async def shutdown(self) -> None:
        """Optional cleanup (e.g., closing browser instances)."""
        pass

    def _make_result(
        self,
        request: FetchRequest,
        start_time: float,
        **kwargs,
    ) -> FetchResult:
        """Helper to create a FetchResult with timing."""
        return FetchResult(
            url=request.url,
            latency=time.monotonic() - start_time,
            strategy_used=self.name,
            **kwargs,
        )
