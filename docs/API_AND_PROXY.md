# Proxify API & Proxy Reference

Technical specifications for interacting with the Proxify v1.0.0.

---

## 1. REST API (Port 8085 in Docker, 8080 natively)

The primary gateway for AI agents and scrapers.

### `POST /fetch` — Universal Bypass Fetcher

Auto-selects the best strategy, handles caching, rate limiting, and escalation.

**Request:**
```json
{
  "url": "https://www.google.com/search?q=scraping",
  "method": "GET",
  "headers": {"X-Custom-Header": "Value"},
  "body": null,
  "params": null,
  "timeout": 30.0,
  "session_id": "my-sticky-session",
  "force_strategy": "nodriver",
  "force_new_session": false,
  "bypass_cache": false
}
```

**Response:**
| Field | Type | Description |
|-------|------|-------------|
| `success` | bool | True if fetched successfully |
| `status_code` | int | HTTP status from target |
| `strategy_used` | string | Which engine won (e.g. `nodriver`, `flaresolverr`) |
| `latency` | float | Total time in seconds |
| `cached` | bool | Served from L1/L2 cache |
| `html` | string | Full decapsulated HTML |
| `final_url` | string | Redirect chain final URL |
| `retries` | int | Number of retries |

### `GET /health`

```json
{"status": "ok", "active_strategies": 10, "l1_cache_size": 42}
```

### `GET /metrics`

Cache hit rates, strategy success rates, latency percentiles.

### `GET /stats`, `/stats/cache`, `/stats/proxies`, `/stats/circuits`

Detailed engine statistics for monitoring.

### `POST /config`

Runtime configuration updates (strategy order, rate limits, etc).

---

## 2. WebSocket API

**Endpoint**: `ws://<host>:8085/ws/fetch`

Designed for batch operations and high-concurrency streaming.

### Multi-Fetch Protocol

1. **Client sends:**
   ```json
   {"url": "https://startpage.com/search?query=test", "options": {"session_id": "agent-1"}}
   ```
2. **Server events:**
   - `{"type": "start"}` — Request accepted
   - `{"type": "result"}` — Full payload
   - `{"type": "error"}` — All engines blocked

---

## 3. HTTP/HTTPS Proxy (Port 8888)

Standard CONNECT proxy. Drop-in for SearXNG, Scrapy, Puppeteer, curl.

### Custom Control Headers

| Header | Effect |
|--------|--------|
| `X-Proxy-Strategy` | Force engine: `nodriver`, `flaresolverr`, `simple`, etc. |
| `X-Proxy-Session` | Sticky session UUID |
| `X-Proxy-Bypass-Cache` | `true` to skip cache |
| `X-Proxy-Timeout` | Custom deadline in seconds |

### Curl Example

```bash
curl -x http://localhost:8888 -k https://example.com
```

---

## 4. Error Codes

| Code | Status | Meaning | Fix |
|------|--------|---------|-----|
| `429` | Throttled | Rate limit hit | Increase `PER_DOMAIN_RATE_LIMIT` |
| `403` | Forbidden | Blocked or captcha | Engine auto-escalates, check proxy health |
| `502` | Strategic Fail | All 10 engines failed | Check target URL reachability |
| `504` | Timed Out | Bypass exceeded timeout | Increase request timeout or `SCRAPLING_TIMEOUT` |

---

> Use the Python or Node.js SDK (`clients/`) for the most resilient integration.
