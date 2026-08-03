"""
Lib++ Core Types — Shared data models for the entire Lib++ system.

Unifies FetchRequest/FetchResult from proxy-orchestrator + nodriver + HTTP/3
+ TLS profiles + WebSocket into one consistent type system.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class StrategyType(str, Enum):
    """All available strategy types in Lib++."""
    CURL_CFFI_PLUS = "curl_cffi_plus"
    NODRIVER = "nodriver"
    TLS_ROTATOR = "tls_rotator"
    HTTP3 = "http3"
    WEBSOCKET = "websocket"
    SCRAPLING_PLUS = "scrapling_plus"
    PLAYWRIGHT_PLUS = "playwright_plus"
    PUPPETEER_PLUS = "puppeteer_plus"
    FLARESOLVERR_PLUS = "flaresolverr_plus"
    DRISSIONPAGE_PLUS = "drissionpage_plus"


class HttpVersion(str, Enum):
    HTTP1 = "http/1.1"
    HTTP2 = "h2"
    HTTP3 = "h3"
    WEBSOCKET = "ws"


@dataclass
class TlsFingerprint:
    """A TLS fingerprint profile (JA3/JA4 + additional signals)."""
    ja3: str
    ja4: str
    ja3n: str = ""       # JA3 with no SNI
    akamai_hash: str = ""  # Akamai-specific fingerprint
    browser: str = "chrome"
    version: str = "131"
    os: str = "windows"
    tls_version: str = "1.3"
    cipher_suites: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    elliptic_curves: list[str] = field(default_factory=list)
    signature_algorithms: list[str] = field(default_factory=list)
    alpn_protocols: list[str] = field(default_factory=lambda: ["h2", "http/1.1"])
    http2_settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class TLSProfile:
    """Full TLS profile with metadata for rotation and learning."""
    name: str
    fingerprint: TlsFingerprint
    weight: float = 1.0
    success_count: int = 0
    failure_count: int = 0
    last_used_domain: str = ""
    last_success_time: float = 0.0
    is_deprecated: bool = False

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.0


@dataclass
class ProxyConfig:
    """Proxy configuration with TLS rotation support."""
    url: str
    protocol: str = "http"
    username: Optional[str] = None
    password: Optional[str] = None
    tls_profile: Optional[str] = None
    country: Optional[str] = None
    sticky_session: bool = False
    max_requests: int = 100
    current_requests: int = 0


@dataclass
class SessionCookie:
    """A session cookie that can be shared across strategies."""
    domain: str
    name: str
    value: str
    path: str = "/"
    secure: bool = True
    http_only: bool = True
    same_site: str = "Lax"
    expires: float = 0.0
    source_strategy: str = ""
    tls_profile_used: str = ""


@dataclass
class FetchRequest:
    """Unified fetch request — superset of proxy-orchestrator's FetchRequest."""
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

    # Lib++ extensions
    tls_profile: Optional[str] = None
    http_version: HttpVersion = HttpVersion.HTTP2
    use_websocket: bool = False
    websocket_message: Optional[str] = None
    require_js: bool = False
    require_canvas_spoof: bool = False
    require_webrtc_block: bool = False
    require_webgl_spoof: bool = False


@dataclass
class FetchResult:
    """Unified fetch result — superset of proxy-orchestrator's FetchResult."""
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

    # Lib++ extensions
    tls_profile_used: Optional[str] = None
    http_version_used: Optional[str] = None
    tls_fingerprint: Optional[TlsFingerprint] = None
    websocket_messages: list[str] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        if self.status_code in (403, 429, 503):
            return True
        # URL pointing at a captcha/block endpoint is always a block
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
        head = self.html.lower()[:15000]
        if head.count("href=") >= 8:
            return False
        block_phrases = [
            "captcha", "cloudflare", "blocked", "access denied",
            "rate limit", "bot detection", "please verify",
            "unusual traffic", "automated requests",
            "google.com/sorry", "/sp/captcha", "challenge-platform",
            "hcaptcha", "recaptcha", "g-recaptcha",
            "enablejs", "noscript", "/httpservice/retry/enablejs"
        ]
        return any(phrase in head for phrase in block_phrases)


@dataclass
class StrategyDecision:
    """The decision output of the enhanced decision engine."""
    strategy: StrategyType
    tls_profile: Optional[str] = None
    http_version: HttpVersion = HttpVersion.HTTP2
    proxy: Optional[ProxyConfig] = None
    use_nodriver: bool = False
    use_js: bool = False
    confidence: float = 0.0
    reasoning: list[str] = field(default_factory=list)


@dataclass
class StrategyMetrics:
    """Per-strategy performance metrics for adaptive selection."""
    strategy: StrategyType
    total_requests: int = 0
    success_count: int = 0
    failure_count: int = 0
    total_latency: float = 0.0
    avg_latency: float = 0.0
    success_rate: float = 0.0
    tls_profile_success: dict[str, float] = field(default_factory=dict)
    domain_success: dict[str, float] = field(default_factory=dict)
    last_error: Optional[str] = None


class BaseLibPlusStrategy(ABC):
    """Abstract base for all Lib++ strategies."""

    @property
    @abstractmethod
    def strategy_type(self) -> StrategyType:
        ...

    @abstractmethod
    async def fetch(self, request: FetchRequest) -> FetchResult:
        ...

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def _make_result(
        self, request: FetchRequest, start_time: float, **kwargs
    ) -> FetchResult:
        return FetchResult(
            url=request.url,
            latency=time.monotonic() - start_time,
            strategy_used=self.strategy_type.value,
            **kwargs,
        )
