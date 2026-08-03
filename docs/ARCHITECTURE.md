# Proxify Architecture

The Proxify is a modular, adaptive system balancing speed, efficiency, and bypass capability across 10 strategy tiers.

---

## Overview

Three main layers:

1. **Gateway Layer** — MITM proxy, FastAPI REST API, and WebSocket server
2. **Decision Engine** — Routes requests through 10 strategies, manages L1/L2 cache, handles session persistence
3. **Strategy Layer** — 10 bypass engines ranging from lightweight curl_cffi to full browser automation

---

## Request Flow

```
Client → HTTP CONNECT :8888  ─┐
Client → POST /fetch    :8085 ─┤
Client → WebSocket /ws/fetch  ┘
                               │
                               ▼
                    ┌──────────────────┐
                    │  Decision Engine │
                    │  (Rate Limit →   │
                    │   Circuit Breaker│
                    │   → Domain Track)│
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  L1 Cache (RAM)  │── Hit? → Return
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  L2 Cache (Redis)│── Hit? → Return
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  10-Tier Strategy│
                    │  Escalation      │
                    │                  │
                    │  1. curl_cffi_plus   │
                    │  2. simple           │
                    │  3. drissionpage_plus│
                    │  4. nodriver         │
                    │  5. tls_rotator      │
                    │  6. scrapling        │
                    │  7. flaresolverr     │
                    │  8. playwright       │
                    │  9. puppeteer        │
                    │  10. puppeteer_plus  │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  Anti-bot Detect │
                    │  + Quality Score │
                    └────────┬─────────┘
                             │
                    ┌────────▼─────────┐
                    │  Return + Cache  │
                    │  + RIL Recording │
                    └──────────────────┘
```

---

## Core Components

### 1. Decision Engine
The brain. Uses circuit breaker, domain tracker, and rate limiter to choose strategies intelligently. Learns from successes/failures per domain. Integrates with Lib++ for enhanced strategies.

### 2. Strategy Layer
10 strategies in priority order. Lightweight first (curl_cffi), full browser last (puppeteer). If a strategy returns an anti-bot response, the engine escalates to the next. Lib++ strategies (1, 3, 4, 5, 10) are loaded from the mounted `/app/Lib_plus_plus` volume.

### 3. Multi-Layer Cache
- **L1 (RAM)**: <1ms lookups, 10K entry limit
- **L2 (Redis)**: Persistent, shared across restarts
- **Binary support**: Images, PDFs, and other binary media

### 4. MITM Proxy
Dynamic SSL certificate generation via self-signed Root CA. Handles `CONNECT` methods transparently. No client-side config needed beyond trusting the Root CA.

### 5. Request Deduplication
If 100 requests hit the same URL simultaneously, only 1 fetch is executed and the result is broadcast to all callers.

---

## Lib++ Enhanced Strategies

| Strategy | From | What It Provides |
|----------|------|------------------|
| `curl_cffi_plus` | Lib++ | TLS rotation + canvas spoofing + JS delegation to nodriver |
| `drissionpage_plus` | Lib++ | Hybrid SessionPage + ChromiumPage with cookie sharing |
| `nodriver` | Lib++ | CDP-direct browser — no `navigator.webdriver`, works in Docker |
| `tls_rotator` | Lib++ | 40+ JA3/JA4 fingerprints with per-domain learning |
| `puppeteer_plus` | Lib++ | Bundled inline script + proxy support, no temp files |

Lib++ is auto-detected at import time via a multi-path fallback (`Lib_plus_plus` → `Lib__` → `Lib*` wildcard).

---

## Ports

| Interface | Default Port | Docker Port | Description |
|-----------|-------------|-------------|-------------|
| HTTP CONNECT Proxy | 8888 | 8888 | Standard HTTP proxy for SearXNG, tools |
| REST API | 8080 | 8085 | FastAPI endpoints (`/fetch`, `/health`, `/stats`) |
| WebSocket | 8080 | 8085 | Real-time streaming (`/ws/fetch`) |
