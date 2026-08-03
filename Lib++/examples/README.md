# Lib++ Integration Guide

Lib++ provides enhanced fetch strategies for the Proxify — nodriver (CDP-direct browser), curl_cffi_plus (TLS rotation + canvas spoofing), tls_rotator (40+ JA3/JA4 fingerprints), drissionpage_plus (hybrid browser), and puppeteer_plus (bundled inline scripts).

---

## Architecture

```
Lib++/
├── __init__.py                  # Package exports
├── core/
│   ├── types.py                 # Unified data models
│   ├── tls_profiles.py          # 40+ JA3/JA4 fingerprints + per-domain learning
│   ├── session_cookie_sharing.py  # Cross-strategy cookie sharing
│   └── http3_client.py          # HTTP/3 + WebSocket via aioquic
├── strategies/
│   ├── nodriver_strategy.py     # CDP-direct browser (no WebDriver leaks)
│   ├── curl_cffi_plus.py        # Enhanced curl_cffi + JS injection + canvas spoof
│   ├── drissionpage_plus.py     # Hybrid SessionPage + ChromiumPage
│   ├── puppeteer_plus.py        # Bundled inline script + proxy, no tempfiles
│   └── tls_rotator.py           # Full TLS rotation engine
├── adapters/
│   ├── orchestrator_adapter.py  # Bridge to proxy-orchestrator Decision Engine
│   └── domain_tracker_plus.py   # TLS-aware domain tracking
├── engine/
│   └── decision_engine_plus.py  # Multi-dimensional strategy selection
└── examples/
    └── README.md                # This file
```

---

## Integration into Proxify

Lib++ is automatically loaded by the Decision Engine at startup. The adapter is imported via a multi-path fallback:

1. `Lib_plus_plus.adapters.orchestrator_adapter` (Docker mount)
2. `Lib__.adapters.orchestrator_adapter` (fallback)
3. Wildcard `Lib*` directory search (last resort)

### Strategy Order

```python
STRATEGY_ORDER = [
    "curl_cffi_plus",    # Lib++ — TLS rotation + canvas spoof
    "simple",            # Original curl_cffi
    "drissionpage_plus", # Lib++ — hybrid SessionPage + ChromiumPage
    "nodriver",          # Lib++ — CDP-direct, no WebDriver leaks
    "tls_rotator",       # Lib++ — full TLS rotation
    "scrapling",         # Original Firefox-based scraper
    "flaresolverr",      # Original Cloudflare solver
    "playwright",        # Original Chromium automation
    "puppeteer",         # Original Node.js browser
    "puppeteer_plus",    # Lib++ — inline script + proxy
]
```

---

## What Lib++ Solves

### curl_cffi disadvantages:
- No JS rendering → nodriver fallback for JS-heavy pages
- No canvas/WebGL/WebRTC spoofing → header-level spoofing + nodriver injection
- Can't execute JavaScript → JS injector + nodriver fallback

### Scrapling disadvantages:
- Firefox-based (different TLS) → Chrome profiles via nodriver
- Slower than curl_cffi → curl_cffi_plus is primary fast path
- Browser overhead → nodriver pool reuses instances

### FlareSolverr disadvantages:
- Does NOT solve Turnstile → nodriver handles Turnstile natively
- Selenium-based (slow) → CDP-direct is much faster
- Only for Cloudflare → multi-strategy works everywhere
- No TLS fingerprinting → full TLS rotation with every request

### Playwright disadvantages:
- `navigator.webdriver` detectable → naturally undefined in nodriver
- CDP leaks possible → nodriver has minimal CDP footprint
- No native TLS fingerprint control → curl_cffi_plus handles TLS
- Heavy (full browser) → pooled nodriver + lightweight curl_cffi first

### Puppeteer disadvantages:
- Subprocess overhead → in-process nodriver
- Same CDP leaks → CDP-direct avoids Puppeteer patterns
- Dead in Docker → nodriver works in Docker natively
