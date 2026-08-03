"""
Central configuration for Proxify.
All settings are loaded from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()


@dataclass
class Config:
    """Main configuration class. All values can be overridden via environment variables."""

    # === Server Ports ===
    PROXY_PORT: int = int(os.getenv("PROXY_PORT", "8888"))
    API_PORT: int = int(os.getenv("API_PORT", "8080"))
    API_HOST: str = os.getenv("API_HOST", "0.0.0.0")

    # === Redis ===
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    REDIS_ENABLED: bool = os.getenv("REDIS_ENABLED", "true").lower() == "true"

    # === FlareSolverr ===
    FLARESOLVERR_URL: str = os.getenv("FLARESOLVERR_URL", "http://localhost:8191/v1")
    FLARESOLVERR_ENABLED: bool = os.getenv("FLARESOLVERR_ENABLED", "true").lower() == "true"
    FLARESOLVERR_MAX_TIMEOUT: int = int(os.getenv("FLARESOLVERR_MAX_TIMEOUT", "60000"))

    # === Scrapling ===
    SCRAPLING_ENABLED: bool = os.getenv("SCRAPLING_ENABLED", "true").lower() == "true"
    SCRAPLING_HEADLESS: bool = os.getenv("SCRAPLING_HEADLESS", "true").lower() == "true"
    SCRAPLING_SOLVE_CLOUDFLARE: bool = os.getenv("SCRAPLING_SOLVE_CLOUDFLARE", "true").lower() == "true"
    SCRAPLING_BLOCK_WEBRTC: bool = os.getenv("SCRAPLING_BLOCK_WEBRTC", "true").lower() == "true"
    SCRAPLING_HIDE_CANVAS: bool = os.getenv("SCRAPLING_HIDE_CANVAS", "true").lower() == "true"

    # === curl_cffi (TLS fingerprint impersonation) ===
    CURL_CFFI_ENABLED: bool = os.getenv("CURL_CFFI_ENABLED", "true").lower() == "true"
    # Comma-separated list of browser impersonation targets for TLS rotation.
    # curl_cffi picks one randomly on each connection attempt.
    # Supports: chrome<version>, safari<version>, edge<version>, firefox<version>
    # Latest open-source targets: chrome124-chrome131, safari17_0, safari18_0
    # Safari included: some anti-bots (old.reddit.com confirmed) 403/tarpit
    # Chrome-family TLS fingerprints but serve Safari-family fingerprints.
    # NOTE: only targets supported by the installed curl_cffi build go here.
    # chrome125-130 are NOT valid targets in curl_cffi 0.15 (verified live:
    # supported chrome = 99/100/101/104/107/110/116/119/120/123/124/131/133a/136/142/145/146).
    CURL_CFFI_IMPERSONATE: str = os.getenv(
        "CURL_CFFI_IMPERSONATE",
        "chrome131,chrome124,safari18_0,safari17_0,chrome146,chrome142,chrome136,edge101,firefox133",
    )

    # === Lib++ (Next-gen strategies) ===
    # curl_cffi_plus — enhanced curl_cffi with JS rendering via nodriver delegation
    CURL_CFFI_PLUS_ENABLED: bool = os.getenv("CURL_CFFI_PLUS_ENABLED", "true").lower() == "true"
    # nodriver — CDP-direct browser (no WebDriver leaks, navigator.webdriver = undefined)
    NODRIVER_ENABLED: bool = os.getenv("NODRIVER_ENABLED", "true").lower() == "true"
    # tls_rotator — full TLS fingerprint rotation with per-domain learning
    TLS_ROTATOR_ENABLED: bool = os.getenv("TLS_ROTATOR_ENABLED", "true").lower() == "true"
    NODRIVER_POOL_SIZE: int = int(os.getenv("NODRIVER_POOL_SIZE", "3"))


    # === Cache ===
    CACHE_ENABLED: bool = os.getenv("CACHE_ENABLED", "true").lower() == "true"
    L1_CACHE_MAX_SIZE: int = int(os.getenv("L1_CACHE_MAX_SIZE", "10000"))
    L1_CACHE_TTL: int = int(os.getenv("L1_CACHE_TTL", "300"))  # seconds
    L2_CACHE_TTL: int = int(os.getenv("L2_CACHE_TTL", "3600"))  # seconds

    # === Proxy Management ===
    UPSTREAM_PROXIES: list[str] = field(default_factory=lambda: [
        p.strip() for p in os.getenv("UPSTREAM_PROXIES", "").split(",") if p.strip()
    ])
    PROXY_ROTATION_STRATEGY: str = os.getenv("PROXY_ROTATION_STRATEGY", "round_robin")  # round_robin, random, sticky
    PROXY_HEALTH_CHECK_INTERVAL: int = int(os.getenv("PROXY_HEALTH_CHECK_INTERVAL", "300"))

    # === Rate Limiting ===
    GLOBAL_RATE_LIMIT: int = int(os.getenv("GLOBAL_RATE_LIMIT", "100"))  # requests per second
    PER_DOMAIN_RATE_LIMIT: int = int(os.getenv("PER_DOMAIN_RATE_LIMIT", "10"))  # per second

    # === Circuit Breaker ===
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = int(os.getenv("CB_FAILURE_THRESHOLD", "5"))
    CIRCUIT_BREAKER_COOLDOWN: int = int(os.getenv("CB_COOLDOWN", "60"))  # seconds as int

    # === GUI Chrome (persistent headful browser in Xvfb VM, CDP :9222) ===
    GUI_CHROME_ENABLED: bool = os.getenv("GUI_CHROME_ENABLED", "true").lower() == "true"

    # === Real session cookies (stealth: cookies + cache so Google/Reddit
    # trust the fetcher instead of serving captchas). Netscape-format file
    # exported from the user's real browser by scripts/brave_cookies.py. ===
    # Path checked at startup + on a refresh interval (see below). Empty or
    # missing file = gracefully skipped (system still works headless).
    COOKIE_FILE: str = os.getenv("COOKIE_FILE", "/app/gui-cookies.txt")
    # Seconds between re-checks of the cookie file for changes (default 30 min).
    COOKIE_REFRESH_INTERVAL: int = int(os.getenv("COOKIE_REFRESH_INTERVAL", "1800"))

    # === STEALTH PERSONA — ONE coherent browser identity for EVERY path ===
    # ---------------------------------------------------------------------
    # Google-class anti-bots correlate fingerprints: when the SAME IP sends
    # your REAL cookies attached to N different browser identities (random
    # TLS family + random UA + random Accept-Language per request), the risk
    # engine flags the whole IP and BOTH the CLI/HTTP path AND the GUI Chrome
    # path get captcha'd. The fix: pin everything to ONE persona that matches
    # the GUI Chrome (Chromium 1228 = Chrome 149; curl_cffi's closest target
    # is chrome146) so every request looks like the same user's browser.
    #
    # PERSONA_PINNED=true  → all HTTP strategies use ONLY the persona (no
    #                        rotation). Recommended for google/reddit.
    # PERSONA_PINNED=false → legacy rotation behavior (multi-fingerprint).
    PERSONA_PINNED: bool = os.getenv("PERSONA_PINNED", "true").lower() == "true"
    # curl_cffi impersonate target for the persona (must exist in the build:
    # chrome99/100/101/104/107/110/116/119/120/123/124/131/133a/136/142/145/146).
    PERSONA_TLS: str = os.getenv("PERSONA_TLS", "chrome146")
    # The exact UA the persona presents — MUST match the TLS target family and
    # MUST match what the GUI Chrome is launched with (see gui_browser.sh).
    PERSONA_UA: str = os.getenv(
        "PERSONA_UA",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    )
    # Fixed Accept-Language — a real user does not change locale per request.
    PERSONA_ACCEPT_LANGUAGE: str = os.getenv("PERSONA_ACCEPT_LANGUAGE", "en-US,en;q=0.9")
    # Fixed Sec-CH-UA client hints matching the persona TLS/UA version.
    PERSONA_SEC_CH_UA: str = os.getenv(
        "PERSONA_SEC_CH_UA",
        '"Not_A Brand";v="24", "Chromium";v="146", "Google Chrome";v="146"',
    )
    # Fixed platform hint (must match the OS in the persona UA).
    PERSONA_PLATFORM: str = os.getenv("PERSONA_PLATFORM", '"Windows"')
    # Fixed viewport width presented by the persona.
    PERSONA_VIEWPORT: int = int(os.getenv("PERSONA_VIEWPORT", "1920"))

    # === Playwright ===
    PLAYWRIGHT_ENABLED: bool = os.getenv("PLAYWRIGHT_ENABLED", "true").lower() == "true"

    # === Puppeteer ===
    PUPPETEER_ENABLED: bool = os.getenv("PUPPETEER_ENABLED", "true").lower() == "true"

    # === Lib++ Puppeteer+ (bundled inline script, proxy support, no tempfile) ===
    PUPPETEER_PLUS_ENABLED: bool = os.getenv("PUPPETEER_PLUS_ENABLED", "true").lower() == "true"

    # === Lib++ DrissionPage+ (hybrid SessionPage + ChromiumPage, fully upgraded) ===
    DRISSIONPAGE_PLUS_ENABLED: bool = os.getenv("DRISSIONPAGE_PLUS_ENABLED", "true").lower() == "true"

    # === Strategy Order (10-tier — keep all for maximum fallback)
    # All 10 strategies kept. Domain memory (in decision_engine.py) reorders
    # them dynamically per domain to skip irrelevant ones first.
    # Pipeline (10-tier + GUI backup): curl_cffi_plus → simple → drissionpage_plus
    # → nodriver → tls_rotator → scrapling → flaresolverr → playwright →
    # puppeteer → puppeteer_plus → gui_chrome (LAST — persistent headful backup)
    STRATEGY_ORDER: list[str] = field(default_factory=lambda: [
        s.strip() for s in os.getenv(
            "STRATEGY_ORDER",
            "curl_cffi_plus,simple,drissionpage_plus,nodriver,tls_rotator,scrapling,flaresolverr,playwright,puppeteer,puppeteer_plus,gui_chrome"
        ).split(",")
    ])

    # === Session Management ===
    SESSION_TTL: int = int(os.getenv("SESSION_TTL", "1800"))  # 30 minutes
    SESSION_CLEANUP_INTERVAL: int = int(os.getenv("SESSION_CLEANUP_INTERVAL", "300"))

    # === Logging ===
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # === Request Deduplication ===
    DEDUP_WINDOW: float = float(os.getenv("DEDUP_WINDOW", "5.0"))  # seconds


# Global singleton
config = Config()
