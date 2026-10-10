# Proxify API & Proxy Reference

Technical specifications for interacting with the Proxify v1.0.0.

---

## 1. REST API (Port 8080)

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
  "bypass_cache": false,
  "use_browser": false,
  "browser": null,
  "body_b64": null,
  "wait_selector": null,
  "raw": false,
  "in_page_upload": false,
  "upload_landing": null,
  "result_url_contains": null
}
```

| Field | Effect |
|-------|--------|
| `use_browser` | pick a real browser (gui_chrome → playwright → puppeteer → nodriver) |
| `browser` | force an exact strategy (alias of `force_strategy`) |
| `body_b64` | binary-safe body (base64), wins over `body` — multipart uploads |
| `wait_selector` | browser strategies wait ≤12 s for this CSS selector before capturing |
| `raw` | return response bytes in `body_b64` even for text, and skip DOM cleaning |
| `in_page_upload` | POST only, `gui_chrome` only: open `upload_landing` (default = origin of `url`), submit the multipart file in a real `<form>`, wait for a URL containing `result_url_contains` (default `/search`), return that page raw. Used for Google Lens, which ties results to the uploading browser session. |

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
| `markdown` | string | Clean markdown (also PDF text) |
| `error` | string | Why it failed, e.g. `HTTP 404 (not found)`, `Google captcha page` |
| `body_b64` | string | Raw bytes for binary responses (images, PDFs, files) and `raw=true` |
| `content_type` | string | Response content type |
| `size` | int | Byte length of `body_b64` payload |
| `metadata` | object | `binary`, `dom_cleaned`, `challenge_detected`, … |

`success` is honest: `false` for HTTP 4xx/5xx and block/CAPTCHA pages.
`404`/`410` are returned as final answers without escalating through every
strategy. Block markers (`/sorry/`, `challenge-platform`, …) are matched
outside inline `<script>` bodies, so real Google pages — whose own JS
mentions `/sorry/index` — are not flagged as blocked.

### `GET /health`

```json
{"status": "ok", "active_strategies": 11, "l1_cache_size": 42}
```

### `GET /metrics`

Cache hit rates, strategy success rates, latency percentiles.

### `GET /stats`, `/stats/cache`, `/stats/proxies`, `/stats/circuits`

Detailed engine statistics for monitoring.

### `GET /cookies/status`, `POST /cookies/refresh`

Imported cookie jar status (domains, counts, source file) and on-demand
hot-reload of the Netscape cookie file (`COOKIE_FILE`) + injection into the
GUI Chrome profile (`gui_chrome_injected` in the response).

### `POST /config`

Runtime configuration updates (strategy order, rate limits, etc).

---

## 2. WebSocket API

**Endpoint**: `ws://<host>:8080/ws/fetch`

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
