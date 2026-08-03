"""
Lib++ — Next-Generation Agentic Search & Anti-Bot Strategy Layer

Solves ALL disadvantages:
  ✅ Cross-strategy session cookie & TLS profile sharing
  ✅ nodriver CDP-direct (no WebDriver leaks, navigator.webdriver = undefined)
  ✅ Full TLS fingerprint rotation (JA3/JA4 + per-domain learning)
  ✅ HTTP/3 + WebSocket support
  ✅ Canvas/WebGL/WebRTC spoofing
  ✅ JS rendering via nodriver delegation
  ✅ TLS profile per-domain learning
  ✅ Proxy support for ALL strategies
  ✅ Puppeteer+ bundled inline script + proxy (no tempfile)
  ✅ Anti-fingerprinting (strips 15+ browser fingerprints)
  ✅ Stealth-browser client (internal, no MCP)
  ✅ Google, Reddit, Wikipedia clean DOM extraction
"""

from .core.types import (
    FetchRequest, FetchResult, TLSProfile, StrategyType,
    StrategyDecision, SessionCookie, TlsFingerprint, HttpVersion,
    ProxyConfig, StrategyMetrics,
)
from .core.tls_profiles import (
    TLSProfileManager, TLS_PROFILES, get_ja3_for_browser, tls_profile_manager,
)
from .core.session_cookie_sharing import (
    SessionCookieSharing, CrossStrategyCookieJar,
    cross_strategy_jar, session_cookie_sharing,
    bridge_to_external_cookie_manager,
)
from .strategies.nodriver_strategy import (
    NodriverStrategy, NodriverPool, StealthBrowserClient,
)
from .strategies.curl_cffi_plus import (
    CurlCffiPlusStrategy, JsInjector, CanvasSpoofer,
)
from .strategies.tls_rotator import (
    TlsRotator, TlsRotationEngine,
)
from .strategies.puppeteer_plus import (
    PuppeteerPlusStrategy,
)
from .strategies.drissionpage_plus import (
    DrissionPagePlusStrategy, DrissionPagePool,
)
from .engine.decision_engine_plus import (
    DecisionEnginePlus,
)
from .adapters.orchestrator_adapter import (
    LibPlusAdapter, build_libplus_strategies,
)
from .adapters.domain_tracker_plus import (
    DomainTrackerPlus,
)
from .processors.dom_to_markdown import (
    DomToMarkdownProcessor, html_to_markdown, attach_markdown_to_result,
)

__version__ = "1.3.0"
__all__ = [
    "FetchRequest", "FetchResult", "TLSProfile", "StrategyType",
    "StrategyDecision", "SessionCookie", "TlsFingerprint", "HttpVersion",
    "ProxyConfig", "StrategyMetrics",
    "TLSProfileManager", "TLS_PROFILES", "get_ja3_for_browser",
    "tls_profile_manager",
    "SessionCookieSharing", "CrossStrategyCookieJar",
    "cross_strategy_jar", "session_cookie_sharing",
    "bridge_to_external_cookie_manager",
    "NodriverStrategy", "NodriverPool", "StealthBrowserClient",
    "CurlCffiPlusStrategy", "JsInjector", "CanvasSpoofer",
    "TlsRotator", "TlsRotationEngine",
    "PuppeteerPlusStrategy",
    "DrissionPagePlusStrategy", "DrissionPagePool",
    "DecisionEnginePlus",
    "LibPlusAdapter", "build_libplus_strategies",
    "DomainTrackerPlus",
    "DomToMarkdownProcessor", "html_to_markdown", "attach_markdown_to_result",
]
