# BUG_POWER — Full-Codebase Bug Audit & Fix Log

Audit date: 2026-08-03
Scope: `proxy-orchestrator`, `ai-captcha`, `ai-captcha-bypass`, `Lib++` (~21k lines Python + shell/Node).

Each entry: **ID · Severity · File:line · Bug · Impact · Fix status**.

Legend: 🔴 Critical · 🟠 High · 🟡 Medium · 🔵 Low. Status: ✅ FIXED / ⬜ OPEN.

---

## 1. proxy-orchestrator (Google/Reddit bypass core)

### PO-1 🔴 Puppeteer strategy reads wrong `process.argv` index
- **File:** `proxy-orchestrator/strategies/puppeteer_strategy.py:28`
- **Bug:** Node script does `JSON.parse(process.argv[1])`, but it is launched as `node <script> <config>` (line 197). With that invocation `argv[1]` is the **script path**, the config JSON is at `argv[2]`. `JSON.parse(scriptpath)` always throws.
- **Impact:** Every Puppeteer fetch returns `success:false`. Puppeteer is tier 9 of the 10-tier pipeline — the deep Google/Reddit bypass fallback is broken.
- **Fix:** `JSON.parse(process.argv[2])`. **FIXED**

### PO-2 🟠 `/stats*` endpoints crash with `AttributeError` (HTTP 500)
- **File:** `gateway/rest_api.py:126,130,136,137,143,148`
- **Bug:** Code accesses `engine.domain_tracker`, `engine.l1_cache`, `engine.rate_limiter`, `engine.proxy_manager`, `engine.circuit_breaker`. In `DecisionEngine.__init__` these are **private**: `_domain_tracker`, `_l1_cache`, `_rate_limiter`, `_proxy_manager`, `_circuit_breaker` (decision_engine.py:100-106). No public aliases exist.
- **Impact:** `/stats`, `/stats/cache`, `/stats/proxies`, `/stats/circuits` all 500. SDK `get_stats()` (client.py:164) broken.
- **Fix:** Use the `_`-prefixed attributes. **status.

### PO-3 🟠 Session cleanup calls nonexistent method — sessions never expire
- **File:** `proxy-orchestrator/engine/decision_engine.py:752`
- **Bug:** Background loop calls `await self._session_manager.cleanup()`. `SessionManager` only defines `cleanup_expired()` (session_manager.py:144).
- **Impact:** Every 300s an `AttributeError` is raised (swallowed by `except`), so sessions/UA/cookies are never expired → unbounded memory growth and stale-session reuse.
- **Fix:** Call `cleanup_expired()`. **status.

### PO-4 🟠 Captcha bridge races the shared Playwright browser + leaks contexts
- **File:** `proxy-orchestrator/engine/captcha_solver_bridge.py:280-281, 498-503` (and `_solve_google_recaptcha_playwright`)
- **Bug:** `PlaywrightStrategy` holds `_browser_lock` for the entire page load to prevent the Node.js EPIPE crash (playwright_strategy.py:196-198). The bridge calls `get_browser()` (lock acquired→released) then freely does `browser.new_context()`/`page.goto()` outside the lock. On any `goto`/`screenshot` exception, `context.close()` is skipped (line 503 / except at 522).
- **Impact:** Concurrent browser access can crash the whole Playwright strategy (EPIPE); contexts leak on errors.
- **Fix:** Acquire the strategy lock around usage; wrap context/page in `try/finally` with `context.close()`. **status.

### PO-5 🟡 Rate-limiter throttle wait is never awaited
- **File:** `proxy-orchestrator/services/rate_limiter.py:86-93`
- **Bug:** Comment says "Release lock and wait" but there is no `await asyncio.sleep(wait)`. Control falls through and `acquire` can return `True` immediately.
- **Impact:** 429/503 backoff is defeated; throttled domains get hammered.
- **Fix:** `await asyncio.sleep(wait)` after releasing the lock. **status.

### PO-6 🟡 `js_challenge_solver` hardcodes port 8085 (config default is 8080)
- **File:** `proxy-orchestrator/engine/js_challenge_solver.py:123`
- **Bug:** `http://localhost:8085/fetch`. `config.API_PORT` defaults to `8080` (config.py:21) and main.py binds `config.API_PORT`.
- **Impact:** In default native deployments the self-fetch gets connection-refused → JS/PoW challenge solving silently fails and always escalates.
- **Fix:** `http://localhost:{config.API_PORT}/fetch`. **status.

### PO-7 🟡 SDK `fetch()` silently drops `session_id`
- **File:** `proxy-orchestrator/clients/python/proxy_orchestrator/client.py:77-118`
- **Bug:** `session_id` (and `force_new_session`) land in `**kwargs` and are never put into `payload`. REST/WS support them.
- **Impact:** Session persistence unavailable via SDK; `test_sdk.py` UA-pinning assertions fail.
- **Fix:** Accept `session_id`/`force_new_session` explicitly and include in payload (both `fetch` and `fetch_async`).

### PO-8 🟡 `main.py` `asyncio.gather(..., return_exceptions=True)` can hang shutdown
- **File:** `proxy-orchestrator/main.py:113-128`
- **Bug:** If uvicorn fails to start, only `shutdown_event.wait()` remains pending; `signal.signal(...)` is replaced by uvicorn's handler so Ctrl+C never sets the event. Process hangs in `finally`, cleanup never runs.
- **Fix:** Cancel the server task when shutdown fires / on first result.

### PO-9 🔵 Proxy "consecutive failure" logic is broken
- **File:** `proxy-orchestrator/services/proxy_manager.py:95-105`
- **Bug:** Requires `failure_count > 3 AND success_count == 0`; `record_success` never resets `failure_count`. A proxy that ever succeeded is never marked unhealthy.
- **Fix:** Reset `failure_count = 0` on success; drop the `success_count == 0` condition.

### PO-10 🔵 MITM cert path traversal (unvalidated domain)
- **File:** `proxy-orchestrator/gateway/mitm_cert.py:83-84`
- **Bug:** `f"{domain}.crt"` / `f"{domain}.key"` interpolate attacker-controlled CONNECT host into a path.
- **Fix:** Sanitize domain (reject anything but `[A-Za-z0-9.-]`).

### PO-11 🔵 Background tasks never cancelled; Redis never closed
- **File:** `proxy-orchestrator/engine/decision_engine.py:204-208, 826-842`; `services/redis_cache.py:109`
- **Bug:** `asyncio.create_task(...)` handles not retained; `shutdown()` doesn't cancel them; `RedisCache.close()` never called.
- **Fix:** Store task handles, cancel in shutdown, `await self._l2_cache.close()`.

### PO-12 🔵 `test_google.py` reads fields not in the response model
- **File:** `proxy-orchestrator/scripts/test_google.py:27-28`
- **Bug:** `data.get('antibot_score')` / `'quality_score'` — `FetchResponseBody` has no such fields (only `metadata.antibot_status`/`quality_usable`).
- **Fix:** Read from `data['metadata']['antibot_status']`.

### PO-13 🔴 Google "trouble accessing" block page not detected as challenge
- **File:** `engine/js_challenge_solver.py`, `engine/captcha_solver_bridge.py`, `engine/decision_engine.py`, `engine/quality.py`
- **Bug:** When Google returns a "trouble accessing" block page (HTML contains `id="yvlrue"`, "If you're having trouble accessing Google Search"), our code doesn't detect it as a challenge. The page passes through as if it were real content, and the quality scorer marks it as usable.
- **Fix:** Added Google block detection patterns to `js_challenge_solver.py` (`CHALLENGE_PATTERNS` and `CAPTCHA_PATTERNS`), added Google block detection to `captcha_solver_bridge.py` (`_HTML_PATTERNS`), added Playwright re-fetch bypass for Google blocks in `decision_engine.py`, and fixed quality scorer to detect challenge pages as not usable.
- **Status:** ✅ FIXED

### PO-14 🔴 tls_rotator returns binary/encrypted data instead of HTML (HTTP/2 brotli)
- **File:** `Lib++/strategies/tls_rotator.py:200`
- **Bug:** The `tls_rotator` strategy uses `httpx.AsyncClient(http2=True)` with `Accept-Encoding: gzip, deflate, br`. When the server responds with brotli-compressed content over HTTP/2, `resp.text` returns binary/encrypted data instead of decompressed HTML. httpx does not properly decompress brotli for HTTP/2 responses.
- **Impact:** Any request routed through `tls_rotator` returned unreadable binary data instead of actual HTML content.
- **Fix:** Added explicit brotli/gzip/deflate decompression after the HTTP response. Now `resp.content` is manually decompressed based on the `Content-Encoding` header before decoding to UTF-8.
- **Status:** ✅ FIXED

---

## 2. ai-captcha (VM headless-Chrome agent)

### AC-1 🟠 Dockerfile omits the ML modules — Jarvis/Moondream/GLM pipeline dead in image
- **File:** `ai-captcha/Dockerfile:48-50`
- **Bug:** Only `agent_watchdog.py`, `livekit_agent.py`, `api_server.py` are `COPY`'d. `jarvis_livekit.py`, `moondream_vlm.py`, `glm_ocr.py`, `vision_clicker.py` are not, but `agent_watchdog.py:1385` does `importlib.import_module("jarvis_livekit")`.
- **Impact:** In the shipped image the primary vision/ML solve path always fails ("No module named 'jarvis_livekit'") and silently falls back to DOM-only.
- **Fix:** Add the missing `COPY` lines. **FIXED**

### AC-2 🟠 `browser.close()` kills the shared VNC Chrome
- **File:** `ai-captcha/python/agent_watchdog.py:1322,1420`; `api_server.py:183`; `jarvis_livekit.py:868`; `livekit_agent.py:240`
- **Bug:** `make_browser(headless=False)` with `DISPLAY` returns a CDP-connected browser (`connect_over_cdp`, line 1171) to the shared Chrome launched by `entrypoint.sh` on port 9222. Calling `browser.close()` on it kills that shared Chrome.
- **Impact:** First solve/screenshot kills the VNC browser the whole VM is built around.
- **Fix:** Only close the browser if this process launched it (track ownership in `make_browser`); otherwise just close the page/context.

### AC-3 🟡 `api_server` job can be stuck `running` forever
- **File:** `ai-captcha/python/api_server.py:144-155, 158-161`
- **Bug:** `except asyncio.TimeoutError` doesn't catch `CancelledError` (disconnect), so `status:"idle"` never runs; also on timeout the wrapped `_run_solve` keeps running while status goes idle → concurrent jobs on shared Chrome.
- **Fix:** `try/finally` to reset status; cancel the background solve task on timeout; retain task handles.

### AC-4 🟡 `ClickResult.is_solved` treats PoW "wait" as solved
- **File:** `ai-captcha/python/vision_clicker.py:69-70`
- **Bug:** `is_solved` returns true whenever `x == -1`. The `wait` action (PoW computing) also uses `x=-1`, so a "computing, wait" LLM response is reported solved.
- **Fix:** `return self.action == "solved"`.

### AC-5 🟡 `verify_solved` keyword scan disabled by any page containing "solved"
- **File:** `ai-captcha/python/agent_watchdog.py:1114-1118`
- **Bug:** `"solved" not in text` is evaluated against whole-page text for every keyword. A page containing the word "solved" anywhere suppresses all captcha checks.
- **Fix:** Match per-keyword.
- **Note:** This conflicts conceptually with AC-3/PO-1 scope (watchdog solve loop); if `verify_solved` is disabled, better fix analyzed per-keyword.

### AC-6 🟡 `_parse_bbox_json` raises `AttributeError` on bare JSON-array responses
- **File:** `ai-captcha/python/moondream_vlm.py:495-508`
- **Bug:** `data.get(...)` on a `list` (bare `[...]`) raises `AttributeError`, only `json.JSONDecodeError` is caught.
- **Fix:** Guard `isinstance(data, list)` first.

### AC-7 🟡 LiveKit screen capture wrong endpoint + muxer
- **File:** `ai-captcha/python/livekit_agent.py:64-72,172`
- **Bug:** WHIP endpoint should be `/rtc/whip` (or per-server), pushed via FFmpeg `-f whip`, not `/rtc/publish` + `rtp_mpegts`.
- **Fix:** Use `/rtc/whip` + `-f whip`.

### AC-8 🟡 `pub_room.connect` has no timeout
- **File:** `ai-captcha/python/jarvis_livekit.py:671-672`
- **Bug:** Publisher connect lacks the `asyncio.wait_for(..., 3.0)` used by the subscriber path.
- **Impact:** LiveKit down → connect hangs the solve request.
- **Fix:** Wrap in `asyncio.wait_for`.

### AC-9 🔵 Blocking calls in `ScreenPublisher` event loop
- **File:** `ai-captcha/python/jarvis_livekit.py:130-149`
- **Bug:** `ImageGrab.grab()` / `subprocess.run()` run synchronously in the coroutine.
 - **Fix:** `run_in_executor`.

---

## TCP Tunnel Architecture (HTTP + HTTPS Duo)

The proxy server (`proxy-orchestrator/gateway/proxy_server.py`) implements a dual-mode TCP tunnel on a single port (default 8888):

### HTTP Mode (non-CONNECT)
- The proxy reads the full HTTP request (headers + body).
- Routes the request through the **DecisionEngine pipeline** (10-tier strategy escalation).
- The pipeline applies anti-bot strategies, captcha solving, DOM cleaning, and Google result extraction.
- The response is returned as a raw HTTP response with cleaned HTML.
- Used by SearXNG for its own searches and for fetching Google search result pages.

### HTTPS CONNECT Mode (TCP Tunnel)
- The proxy creates a raw TCP tunnel using Happy Eyeballs v2 (connection racing).
- Resolves all IPs (A + AAAA) for the target host.
- Races connections with staggered 200ms delays.
- Pipes encrypted bytes bidirectionally between client and remote server.
- The proxy does NOT decrypt or inspect HTTPS traffic. No anti-bot processing happens.
- Used by SearXNG to reach HTTPS sites directly through the proxy.

### How Google Scraping Works via the TCP Tunnel
1. SearXNG sends an HTTP request to the proxy (non-CONNECT mode).
2. The proxy routes the request through the DecisionEngine pipeline.
3. The pipeline tries 10 strategies in order: curl_cffi_plus, simple, drissionpage_plus, nodriver, tls_rotator, scrapling, flaresolverr, playwright, puppeteer, puppeteer_plus.
4. For Google searches, the `simple` strategy uses a two-phase approach:
   - Phase 1: Legacy IE11 UA to get non-JS Google HTML.
   - Phase 2: Modern Chrome UA if Phase 1 fails.
5. After fetching, `clean_dom()` extracts Google SERP results and generates markdown via `make_google_results_markdown()`.
6. The cleaned HTML and markdown are returned to SearXNG.

### Key Insight
- The TCP tunnel for HTTPS (CONNECT) is a transparent tunnel - it does NOT process content.
- The TCP tunnel for HTTP (non-CONNECT) goes through the full DecisionEngine pipeline.
- To get Google results and convert them to markdown, the client must use HTTP proxy mode (not HTTPS CONNECT tunnel).
- The REST API (`/fetch` endpoint) also goes through the DecisionEngine pipeline and returns JSON with cleaned HTML and markdown metadata.

---

## 3. ai-captcha-bypass (42-type captcha solver)

### BYP-1 🔴 `puzzle_solver.py` imports a function that doesn't exist
- **File:** `ai-captcha-bypass/puzzle_solver.py:13,116`
- **Bug:** Imports `ask_puzzle_distance_to_chatgpt` from `ai_utils` — never defined (only `ask_puzzle_distance_to_gemini`/`_ollama`; OpenAI uses `ask_puzzle_correction_direction_to_openai`).
- **Impact:** `from puzzle_solver import solve_geetest_puzzle` raises `ImportError` at import time → `main.py` CLI crashes; `my_solvers.py` import chain broken transitively. The `--provider openai` path is dead.
- **Fix:** Implemented `ask_puzzle_distance_to_openai` in `ai_utils.py` following the Gemini/Ollama pattern, and wired it into `puzzle_solver.py`. **FIXED**

### BYP-2 🔴 Sync CPU-bound PoW loops block the async event loop
- **File:** `ai-captcha-bypass/api_service.py:495-520, 589-598, 699-722, 782-801, 1416-1420,1440,1486`
- **Bug:** `solve_turnstile`/`solve_flare`/`solve_altcha`/`solve_friendly` / `_anubis_pow_solve` brute-force SHA-256 in `async` handlers with no `run_in_executor`. `_anubis_pow_solve` has **no nonce cap**.
- **Impact:** A single PoW request head-of-line-blocks the entire FastAPI service (DoS). Anubis difficulty from attacker content is unbounded.
- **Fix:** Offload to `loop.run_in_executor` (as `_enterprise_pow` does at :1546); cap Anubis nonce; validate difficulty.

### BYP-3 🔴 Hardcoded credentials in source
- **File:** `ai-captcha-bypass/scraping_test.py:37-38`
- **Bug:** Real Facebook `email`/`pass` committed in code.
- **Fix:** Remove; load from env; rotate credentials.

### BYP-4 🟠 `eval()` on LLM output in `solve_math`
- **File:** `ai-captcha-bypass/api_service.py:267`
- **Bug:** `solution = str(eval(safe))` with `re.sub(r'[^0-9+\-*/() ]','',eq)`. Expressions like `9**9**9` cause unbounded CPU/memory (DoS); `eval` on untrusted input is unsafe.
- **Fix:** Use a safe arithmetic evaluator (operator/shunting-yard / `ast.parse` with numeric-only).

### BYP-5 🟠 `navigate` raises `UnboundLocalError` when `goto` fails
- **File:** `ai-captcha-bypass/browser_mcp_server.py:81-96`
- **Bug:** `except: pass` then `resp.status` — `resp` unbound if `goto` raised.
- **Fix:** init `resp = None`.

### BYP-6 🟡 `mouse_click` clicks (0,0) not current position
- **File:** `ai-captcha-bypass/browser_mcp_server.py:142`
- **Bug:** `_page.mouse.click(0,0)` moves pointer to top-left. Docstring says "at current position".
- **Fix:** `mouse.down()`/`mouse.up()`.

### BYP-7 🟡 `_do_solve` clobbers auto-detected `captcha_type`
- **File:** `ai-captcha-bypass/api_service.py:1965`
- **Bug:** `solve_auto` returns the real detected type, then `result.captcha_type = 'auto'` overwrites it. `/solve/auto` and MCP `classify_captcha` always report `auto`. Added in the file: `req.captcha_type = captcha_type`.
- **Fix:** Only set when not already resolved.

### BYP-8 🟡 Whisper blocking load + temp file not cleaned on error
- **File:** `ai-captcha-bypass/api_service.py:135-149`
- **Bug:** `whisper.load_model()` runs on every request in the event loop (no cache, unlike `whisper_utils.py`); `os.unlink(path)` not in `finally`.
- **Fix:** Cache a process-global model; unlink in `finally`.

### BYP-9 🟡 Invalid Selenium selector in `main.py`
- **File:** `ai-captcha-bypass/main.py:114-115`
- **Bug:** `By.CLASS_NAME, "mtcap-noborder.mtcap-inputtext.mtcap-inputtext-custom"` (dotted string as one class) never matches → `TimeoutException`; MTCaptcha flow can't finish.
- **Fix:** `By.CSS_SELECTOR, ".mtcap-noborder.mtcap-inputtext.mtcap-inputtext-custom"`.

### BYP-10 🔵 `main.py` `create_success_gif` uses undefined `Image`/`datetime`
- **File:** `ai-captcha-bypass/main.py:28-55`
- **Bug:** `Image.open`, `datetime.now` but no imports in `main.py`.
- **Fix:** Add imports.

### BYP-11 🔵 `puzzle_solver` unvalidated `best_idx` / `direction`
- **File:** `ai-captcha-bypass/puzzle_solver.py:172,211,233`
- **Bug:** `scan_screenshots[best_idx]` can `IndexError`; `'+' in None` TypeError when `dir_str` empty; negative-position skip leaves <3 screenshots.
- **Fix:** clamp/validate indices, guard empty direction.

### BYP-12 🔵 `/solve` default `captcha_type="text"` not auto
- **File:** `ai-captcha-bypass/api_service.py:63,1952`
- **Bug:** `SolveRequest.captcha_type: str = "text"` — POST `/solve` with no type solves as plain text instead of auto-classifying.
- **Fix:** default to `"auto"`.

### BYP-13 🔵 whisper_utils load race
- **File:** `ai-captcha-bypass/whisper_utils.py:25-30`
- **Bug:** check-then-act double-load.
- **Fix:** lock.

### BYP-14 🔵 reCAPTCHA v3 `post_data` not URL-encoded
- **File:** `ai-captcha-bypass/api_service.py:360`
- **Bug:** raw `site_url`/`site_key` injected into form body.
- **Fix:** `urllib.parse.urlencode`.

---

## 4. Lib++

### LB-1 🔴 view metrics strategy `argv` off-by-one
- **File:** `Lib++/strategies/puppeteer_plus.js:45`
- **Bug:** `JSON.parse(process.argv[1])` but launched as `node <script> <config>` (puppeteer_plus.js:373-377). Parsing the script path throws → every `PuppeteerPlus.fetch` fails.
- **Fix:** `process.argv[2]`. **FIXED**

### LB-2 🟠 Proxy flag pushed after `puppeteer.launch`
- **File:** `Lib++/strategies/puppeteer_plus.js:125-133`
- **Bug:** `launchArgs.push('--proxy-server=...')` happens *after* `browser = await puppeteer.launch(...)` (line 64). Proxy is never applied.
- **Fix:** Push proxy arg before launch; also `page.authenticate` must come after navigation context creation (or set via launch `proxy`).

### LB-3 🟠 `decision_engine_plus.js` reads TLS/HTTP-version from wrong field
- **File:** `Lib++/engine/decision_engine_plus.js:147-149, 166`
- **Bug:** `result.metadata.get("tls_profile_used")` / `get("http_version_used")`. `FetchResult.metadata` never contains these — the real fields are `result.tls_profile_used` and `result.http_version_used` (types.py:151-152).
- **Impact:** Per-domain TLS/HTTP-version learning collapses to `unknown`/`h2`. The core adaptive feature is dead.
- **Fix:** Use the dataclass fields. **FIXED**

### LB-4 🟡 All HTTP strategies ignore `method` / `body` (always GET)
- **Files:** `Lib++/strategies/curl_cffi_plus.py:299`, `tls_rotator.ts:203-206`, `core/http3_client.py:203-205, 217-219`
- **Bug:** `client.get(request.url, ...)` hardcoded; `request.method`/`request.body` ignored.
- **Impact:** POST/PUT/DELETE with body silently sent as GET.
- **Fix:** dispatch on `request.method`, pass `content=request.body`.

### LB-5 🟡 Nodriver / DrissionPage pools drop proxy
- **Files:** `Lib++/strategies/nodriver_strategy.py:272-296` (:372), `drissionpage_plus.py:85-90`
- **Bug:** `proxy` param accepted but never applied to browser config; reuse matching ignores proxy/UA.
- **Fix:** launch config; include proxy/UA in reuse key.

### LB-6 🟡 TLS rotation timer never fires under load
- **File:** `Lib++/core/tls_profiles.py:296-304`
- **Bug:** `_last_rotation[domain] = now` on every `select_profile` call, so `elapsed > 300` never true.
- **Fix:** only update timestamp on actual rotation. **FIXED**

### LB-7 🟡 curl_cffi impersonate target decoupled from selected TLS profile
- **File:** `Lib++/strategies/curl_cffi_plus.py:277,289-293`
- **Bug:** records `tls_profile.name` while using `self._next_impersonate()` (independent rotation).
- **Fix:** map selected profile → impersonate target. **FIXED**

### LB-8 🟡 `orchestrator_adapter` absolute import at runtime
- **File:** `Lib++/adapters/orchestrator_adapter.js:76`
- **Bug:** `from strategies.base import FetchResult` (proxy-orchestrator's package) breaks when orchestrator isn't on `sys.path`.
- **Fix:** optional/relative import fallback.

### LB-9 🟡 missing hard deps in `setup.py`
- **File:** `Lib++/setup.py:35-51`, `requirements.txt`
- **Bug:** `bs4` and `markdownify` imported by `processors/dom_to_markdown.py` but absent from `install_requires`.
- **Fix:** add to deps.

### LB-10 🔵 H3 redundant `handle_event` no-op
- **File:** `Lib++/core/http3_client.py:296`
- **Fix:** remove line.

### LB-11 🔵 `PuppeteerPlusStrategy` returns `playwright_plus`
- **File:** `Lib++/strategies/puppeteer_plus.ts:274`
- **Bug:** `StrategyType.PLAYWRIGHT_PLUS` — no `PUPPETEER_PLUS` enum.
- **Fix:** add enum + return it.

### LB-12 🔵 DOM comment-removal predicate dead
- **File:** `Lib++/processors/dom_to_markdown.ts:128`
- **Fix:** match `isinstance(text, Comment)`.

### LB-13 🔵 wrong var in rotation log
- **File:** `Lib++/strategies/tls_rotator.ts:97`
- **Fix:** log original profile.

### LB-14 🟡 blocked-case loses `last_result` ("No strategies available")
- **File:** `Lib++/engine/decision_engine_plus.ts:157-168,181-188`
- **Bug:** blocked results `break` without assigning `last_result`.
- **Fix:** track last result incl. blocked.

### LB-15 🟡 in-place header mutation
- **File:** `Lib++/engine/decision_engine_plus.ts:136-138`
- **Bug:** `request.headers["Cookie"]=...` mutates caller's object, leaking across retries.
- **Fix:** copy headers.

### LB-16 🔵 proxy http-only
- **File:** `Lib++/strategies/curl_cffi_plus.ts:356`
- **Fix:** add `"http"` key.

### LB-17 🟡 `require_js` read from wrong field
- **File:** `Lib++/strategies/drissionpage_plus.ts:293`
- **Bug:** `request.metadata.get("require_js")` — it's a top-level `FetchRequest` field.
- **Fix:** `request.require_js`.

### LB-18 🔵 cookie domain includes port
- **File:** `Lib++/strategies/curl_cffi_plus.ts:122,148`
- **Fix:** use `.hostname`.

### LB-19 🔵 swallowed cookie-bridge exceptions
- **File:** `Lib++/core/session_cookie_sharing.js:86-87,139-140`
- **Fix:** log / narrow except.

### LB-20 🟡 deprecated `websockets.extra_headers`
- **File:** `Lib++/core/http3_client.ts:238`
- **Bug:** `extra_headers=` removed/deprecated in websockets ≥13/14.
- **Fix:** use `additional_headers` w/ fallback.

---

### NEW BUGS FOUND (2026-08-03)

### NB-1 🔴 `js_challenge_solver.py` hardcodes port 8085 instead of `config.API_PORT`
- **File:** `proxy-orchestrator/engine/js_challenge_solver.py:134`
- **Bug:** `_fetch_through_playwright()` hardcodes `http://localhost:8085/fetch` but `config.py` sets `API_PORT` default to `8080`. This breaks all PoW solving in default deployments — the self-fetch to solve Reddit's JS PoW challenge fails with a connection error, and the engine silently escalates to the next strategy instead of solving the challenge.
- **Fix:** Changed to `f"http://localhost:{config.API_PORT}/fetch"` and added `from config import config` import.
- **Status:** FIXED

### NB-2 🟠 `dom_to_markdown.py` `_detect_site()` broken for `www.google.com`
- **File:** `Lib++/processors/dom_to_markdown.py:283-289`
- **Bug:** After stripping `www.` from the domain, `google.com` does not end with `.google.` (it ends with `.com`). The Google site-specific cleaner never triggers for `www.google.com` URLs.
- **Fix:** Changed matching logic to use `pattern.rstrip(".") in domain` for prefix patterns like `"google."`, and proper suffix matching for exact patterns like `"reddit.com"`.
- **Status:** FIXED

### NB-3 🟡 `dom_to_markdown.py` no captcha/block page detection
- **File:** `Lib++/processors/dom_to_markdown.py`
- **Bug:** When HTML is a captcha or block page (Google "trouble accessing", Reddit "Please wait for verification"), the markdown extraction returned empty content with `success=False` but no indication of why.
- **Fix:** Added `_BLOCK_PAGE_INDICATORS` list with 30+ patterns including English, Bengali, and other language variants. Added `_detect_block_page()`, `_detect_captcha()`, and `_is_google_block_page()` helper functions. Block page detection runs before site-specific cleaning and returns a meaningful error message.
- **Status:** FIXED

### NB-4 🟡 `dom_to_markdown.py` `_SITE_HANDLERS` matching inconsistent
- **File:** `Lib++/processors/dom_to_markdown.py:274`
- **Bug:** `"google."` used prefix matching while `"reddit.com"` and `"wikipedia.org"` used exact matching. The `_detect_site()` method was inconsistent across site types.
- **Fix:** Unified matching logic with proper suffix-based detection for all patterns.
- **Status:** FIXED

### NB-5 🟡 REST API `/fetch` missing markdown extraction
- **File:** `proxy-orchestrator/gateway/rest_api.py`
- **Bug:** The REST API `/fetch` endpoint returned only raw HTML, not markdown. Clients had to run their own markdown extraction.
- **Fix:** Added `html_to_markdown()` import from `Lib_plus_plus.processors.dom_to_markdown`. Added `markdown` and `markdown_metadata` fields to `FetchResponseBody`. The `/fetch` endpoint now calls `html_to_markdown()` after `engine.fetch()` and includes the result in the response.
- **Status:** FIXED

---

## Session Fixes — 2026-08-03 (evening) — Google/Reddit death + Docker fingerprint hardening

### SF-1 🔴 Google dead = PO-13's "block detection" false positives
- **Files:** `engine/js_challenge_solver.py`, `engine/quality.py`, `engine/captcha_solver_bridge.py`, `Lib++/processors/dom_to_markdown.py`
- **Bug:** The PO-13 fix treated `yvlrue`, `sca_esv=`, `emsg=SG_REL`, and the text "trouble accessing" as Google block markers. In reality **all four appear on Google's NORMAL pages** — the enablejs shell and even real SERPs contain the hidden `<div id="yvlrue">` ("If you're having trouble accessing Google Search, click here"), and `sca_esv=`/`emsg=SG_REL` are standard URL params. The markdown extractor additionally matched the title pattern `"Google Search"` — the normal title of every results page.
- **Impact:** Every Google fetch was classified as captcha/block → the ~10s interactive captcha bridge fired on JS strategies → all strategies exhausted → 502. Markdown extraction returned `blocked_...` for every Google SERP.
- **Fix:** Removed the false-positive markers from CHALLENGE_PATTERNS, CAPTCHA_PATTERNS, `_HTML_PATTERNS`, quality challenge_phrases, and `_BLOCK_PAGE_*` lists. Real Google blocks are now detected via `/sorry/` URL, "unusual traffic" text, and recaptcha patterns. **FIXED**

### SF-2 🔴 Playwright "not available" (js_challenge_solver dead) = browser build mismatch + Ubuntu 26.04
- **File:** `strategies/playwright_strategy.py`
- **Bug:** venv playwright 1.60 expects chromium build **1223** but disk had **1228**; and `playwright install` refuses "ubuntu26.04-x64". Result: every Playwright/scrapling launch threw → PlaywrightStrategy `_available=False` → `solve_js_challenge()` (Reddit PoW path) could never run. Puppeteer/Puppeteer+ also dead (missing global `puppeteer-extra`).
- **Fix:** Self-healing `_ensure_browser_installed()`: tries `python -m playwright install chromium`, falls back to a **manual Chrome-for-Testing download** (bypasses the OS whitelist) using the exact build from `browsers.json`. Hardened launch args + extended stealth init script (WebGL vendor/renderer spoof, WebRTC candidate strip, hardwareConcurrency/deviceMemory). **FIXED** (verified: PlaywrightStrategy initialized, example.com loads)

### SF-3 🟠 Reddit pipeline hangs = untimed browser navigation
- **Files:** `Lib++/strategies/drissionpage_plus.py`, `Lib++/strategies/nodriver_strategy.py`
- **Bug:** `page.get()` / `tab.get()` have no timeout → a slow/challenged Reddit wedged the pipeline until the global fetch timeout (80s).
- **Fix:** Wrapped navigation in `asyncio.wait_for(timeout=request.timeout+5)`. **FIXED**

### SF-4 🟠 Docker image could not build
- **Files:** `proxy-orchestrator/Dockerfile`, `proxy-orchestrator/docker-compose.yml`, root `.dockerignore`
- **Bug:** `COPY ../Lib++ /app/Lib++` is invalid (parent-dir COPY outside context) → image unbuildable. Also `pip install -e /app/Lib++` with old pip (legacy egg-link) cannot handle the `Lib++ → Lib_plus_plus` package_dir rename → `ModuleNotFoundError: Lib_plus_plus`; and `brotli` was an undeclared dep of `tls_rotator.py`.
- **Fix:** build context → repo root; `COPY proxy-orchestrator /app` + `COPY Lib++ /app/Lib++`; upgrade pip/setuptools; `ln -sfn` fallbacks for `Lib_plus_plus`/`Lib__`; `brotli` added to `Lib++/setup.py` + `requirements.txt`; fonts (Liberation/DejaVu/Noto) + tzdata installed; compose adds `shm_size: 1gb`, `init: true`, `PLAYWRIGHT_BROWSERS_PATH`, `TZ`. **FIXED** (image builds, container healthy, all strategies registered)

### SF-5 🟠 Container fingerprint hardening (hide Docker)
- **Files:** `strategies/playwright_strategy.py`, `Lib++/strategies/nodriver_strategy.py`, `Lib++/strategies/drissionpage_plus.py`, `Dockerfile`
- **Fix:** removed `--disable-gpu` (SwiftShader renderer = container giveaway; renderer spoofed via init script); `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` (stops leaking the internal 172.x IP via WebRTC); `--no-first-run`/`--disable-background-networking`/etc.; fonts installed (empty font list is a container signal); anti_fingerprint.js injected by nodriver/drissionpage already covers canvas/WebGL/WebRTC/plugins. DrissionPage/nodriver now **reuse the Playwright-installed Chromium** in the container instead of downloading their own (no system Chrome in Docker). **FIXED** — container now behaves identically to host.

### SF-6 🟡 Google empty stub accepted as success
- **File:** `engine/decision_engine.py`
- **Bug:** Google's enablejs/consent stub (contains only the hidden yvlrue div) scored antibot=25 → accepted → 371B of useless HTML returned as success.
- **Fix:** for Google searches, escalate unless the result has real content (`<h3` or `google_results` metadata); force-accept only when real content exists. **FIXED**

### SF-7 🟡 js_challenge_solver infinite recursion risk
- **Files:** `engine/js_challenge_solver.py`, `engine/decision_engine.py`
- **Bug:** `solve_js_challenge()` POSTs to the REST `/fetch` with `force_strategy=playwright`; if playwright returns another challenge page, the nested pipeline re-detects it and calls the solver again — unbounded recursion, each layer +120s.
- **Fix:** nested fetches carry `X-Orchestrator-Challenge-Solve: skip`; the pipeline skips challenge solving when present. **FIXED**

### SF-8 🟠 puppeteer_plus hangs on any site with long-lived connections (networkidle2)
- **File:** `Lib++/strategies/puppeteer_plus.py`
- **Bug:** The strategy hardcoded `waitUntil: "networkidle2"` and the inline Node script defaulted to `networkidle2`. GitHub Pages / analytics / websocket sites (e.g. `www.furylogic.com`, the user's own unprotected site) keep connections open, so `networkidle2` never fires → every `puppeteer_plus` fetch hit the full 45s navigation timeout.
- **Fix:** Default `waitUntil` → `domcontentloaded` in both the Python config and the Node script; keep `networkidle2` only for Google SERP URLs (which need JS to settle). Verified in container: `www.furylogic.com` via puppeteer_plus went from 45s timeout → **1.6s, 200, 58KB**. **FIXED**

### SF-9 🟡 Puppeteer strategies dead in container — npm modules + browser verification
- **File:** `proxy-orchestrator/Dockerfile` (step 4 already had `npm install -g puppeteer puppeteer-extra puppeteer-extra-plugin-stealth`), verified live in `po-test` container via terminal (no rebuild)
- **Bug:** Container runs were ending before Puppeteer tiers could be exercised; on the host the global `puppeteer-extra` npm module was never installed → `PuppeteerStrategy`/`PuppeteerPlusStrategy` reported "not available" everywhere.
- **Fix:** Installed the npm stack **inside the running container** via `docker exec` (`npm install -g puppeteer puppeteer-extra puppeteer-extra-plugin-stealth`, idempotent) — the built image already had it via the Dockerfile, and Puppeteer's bundled Chrome is at `/root/.cache/puppeteer`. Verified: `PPTR_LAUNCH_OK title=Example Domain`; both strategies log `initialized (puppeteer-extra available)`; real fetches through the DecisionEngine: `puppeteer` → furylogic 200/58KB in 1.9s, `puppeteer_plus` → 200/58KB in 1.6s. **FIXED** (container) — host still lacks the global npm module (host is not the target env).

### SF-10 🔴 Docker fingerprint leaks made Reddit challenge the container browser
- **Files:** `Lib++/scripts/anti_fingerprint.js`, `Lib++/strategies/drissionpage_plus.py`, `Lib++/strategies/nodriver_strategy.py`, `proxy-orchestrator/strategies/playwright_strategy.py`
- **Bugs found by live fingerprint probing inside the container (fp_probe):**
  1. **anti_fingerprint.js infinite recursion** — `Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => Math.max(4, Math.min(16, navigator.hardwareConcurrency || 8))})` reads the *overridden* property → `RangeError: Maximum call stack size exceeded` the moment any page JS reads `hardwareConcurrency`. Any anti-bot JS reading it crashes → automation signal. Same trap in `deviceMemory`.
  2. **Non-persistent injection** — DrissionPage used `page.run_js(afp)` and nodriver used `tab.evaluate(afp)`, which execute once on the about:blank document and are WIPED on navigation → the target page (Reddit) saw ZERO spoofing. Fixed via CDP `Page.addScriptToEvaluateOnNewDocument` (DrissionPage: `page.run_cdp(...)`; nodriver: `tab.send(cdp.page.enable())` + `add_script_to_evaluate_on_new_document`) — same mechanism as Playwright's `add_init_script`.
  3. **HeadlessChrome UA leak** — both strategies launched with the browser default UA containing `HeadlessChrome/149.0.0.0` (instant bot giveaway). Now pinned to a realistic desktop Chrome UA at launch.
  4. **TZ=UTC leak** — container TZ env = UTC; DrissionPage/nodriver never emulated a timezone → `Intl.DateTimeFormat().resolvedOptions().timeZone` = "UTC" for a Bangladesh IP. Now emulated to Asia/Dhaka via CDP (Playwright got Asia/Dhaka added to its timezone pool).
  5. **hardwareConcurrency=16+** — headless containers report many cores; clamped to 4–8 to match a typical desktop.
- **Result (verified in container):** all three browser strategies now report real Chrome UA, `webdriver: undefined`, `window.chrome` present, TZ Asia/Dhaka, cores=8, WebRTC IPs hidden. **REDDIT went from constant PoW-challenge/90s-timeout → `success=True` in 1.8s via simple (real posts)**.
- **Also fixed:** `is_js_challenge()`/`is_captcha_page()` false-positives on REAL large pages — genuine Reddit HTML contains the literal strings `recaptcha`/`g-recaptcha` (JS bundle refs), so a 1MB real page was misclassified as a captcha and the pipeline burned 30s+ "solving" it, then escalated → 90s global timeout. Added `_MAX_CHALLENGE_PAGE_SIZE = 200_000` guard (challenge pages are small 5-50KB; real content is bigger). **FIXED**

### SF-11 🟠 `simple` strategy stale-pool hang starved the pipeline
- **File:** `proxy-orchestrator/strategies/simple.py`
- **Bug:** `simple` uses ONE shared persistent curl_cffi httpx client. When the pooled keepalive connection to a host goes stale (server silently closed it), the next request hangs until the 30s/60s client timeout — even though raw network is fast (curl = 0.3s). In the pipeline this ate the whole 90s global budget, so the browser strategies never got a chance (Reddit/cloud timed out at 90s while raw curl returned in <2s).
- **Fix:** hard per-request cap `min(request.timeout, 20)` via `asyncio.wait_for`; on timeout, `_reset_client()` closes+drops the shared client so the next fetch re-initializes with a fresh connection. **FIXED** — Reddit went from 90s timeout → 1.8s success.

### SF-12 🟠 JS-heavy domains won by non-JS strategies → near-empty markdown ("success" with useless content)
- **File:** `engine/decision_engine.py`
- **Bug:** `simple`/`curl_cffi_plus` run FIRST in the pipeline. For JS-heavy/SPA domains (Reddit, Nextcloud, Twitter, etc.) they return the JS shell — 128KB of HTML that is 99% script with <100 words of extractable markdown — and it scores well on antibot (ab=25) so it's ACCEPTED. The browser strategies (drissionpage/playwright/puppeteer) that actually render the content never got a chance. Result: `success=True` with 62B of markdown for Reddit — the exact "false true" the user distrusted.
- **Fix:** for known JS-heavy domains, reorder the pipeline so JS-capable strategies run FIRST (`drissionpage_plus → playwright → puppeteer...`), falling back to HTTP strategies only if browsers fail. **FIXED** — cloud.furylogic markdown went 11B → 152B (real login form via playwright); www.furylogic 7.9KB via playwright. Reddit's thin markdown now is server-side throttling (Reddit serves a post-less page even to a real browser after heavy testing; it returned full content at 11:53 `SUCCESS via simple in 1.33s ab=25 q=100`).

### SF-13 🔴 old.reddit.com dead = Chrome-only TLS fingerprints + false-positive challenge detection (NOT Docker)
- **Files:** `Lib++/core/tls_profiles.py`, `Lib++/strategies/curl_cffi_plus.py`, `proxy-orchestrator/strategies/simple.py`, `proxy-orchestrator/config.py`, `proxy-orchestrator/.env`, `proxy-orchestrator/engine/decision_engine.py`, `proxy-orchestrator/engine/js_challenge_solver.py`, `Lib++/core/types.py`, `proxy-orchestrator/strategies/base.py`
- **Bugs found by live TLS fingerprint probing (curl_cffi impersonation matrix on old.reddit.com):**
  1. **Anti-bot discriminates by TLS fingerprint family, not container.** Plain curl (OpenSSL) → tarpit (0 bytes, stall); `chrome*` impersonation → 403 (189KB block page) or timeout; **`safari18_0`/`safari17_0` → 200 with 134KB REAL content**. The pipeline was Chrome-locked: `curl_cffi_plus` hardcoded `preferred_browser="chrome"`, `simple` rotated only `chrome124-131`, and the Safari profile in TLS_PROFILES would have emitted an invalid target (`safari17` ≠ curl_cffi's `safari17_0`).
  2. **old.reddit.com misclassified as JS-heavy** — `"reddit.com" in domain` matched `old.reddit.com`, so browser strategies ran FIRST and hit Reddit's reCAPTCHA (`6LeTnxkTAAAAAN9Q`) → captcha bridge burned the budget → 80s global timeout. But old.reddit is the CLASSIC server-rendered interface; HTTP strategies are correct for it.
  3. **Real content flagged as blocked/captcha** — old.reddit's classic UI embeds the reCAPTCHA login `<script>` on every page, so a 134KB real listing contains the literal string "recaptcha". `FetchResult.is_blocked` (both Lib++ types.py and strategies/base.py) and `is_js_challenge`/`is_captcha_page` all phrase-scan it → real content discarded even with status=200 (log: `safari18_0 blocked on old.reddit.com (status=200)`).
- **Fix:**
  1. Added `safari18_mac`/`safari18_4_mac` profiles, corrected Safari version format (`17_0`/`18_0`/`18_4`), added `_normalize_target()` so profile versions map to valid curl_cffi targets.
  2. `curl_cffi_plus.fetch()` now walks a **browser-family fallback chain** (`chrome → safari → firefox → edge`) with per-domain learning: blocked/tarpitted families get `record_failure` and the next family is tried; the family that actually serves content wins and is remembered for the domain. UA + Sec-CH-UA headers now match the impersonated family (Safari TLS + Chrome UA was itself a signal). Per-family attempt capped at 12s so a tarpit can't eat the pipeline budget.
  3. `simple.py` stores its current impersonate target and matches UA/Sec-CH-UA to it; Safari/Firefox/Edge added to `CURL_CFFI_IMPERSONATE` in config.py + .env (container).
  4. `decision_engine.py`: `old.reddit.com` excluded from JS-heavy reordering; explicitly HTTP-strategies-first for it.
  5. Challenge detectors (`is_js_challenge`, `is_captcha_page`, both `is_blocked`) now treat link-rich pages (`≥8 href=` in first 15KB) as real content regardless of "captcha" strings — challenge pages are small AND link-sparse script wrappers.
- **Result (verified in container):** old.reddit.com via default pipeline → `success=True status=200` in **2.3s** via `curl_cffi_plus` (78KB html, 12.5KB markdown, 876 words of REAL posts) and **14.4s** via `simple` (13.5KB markdown, 1024 words). Warm retry 13.5s/1024 words. **FIXED**
- **Review hardening (code-reviewer feedback):**
  1. `edge131` is NOT a valid curl_cffi 0.15 target (verified live: `ImpersonateError`) — config/.env now use `edge101`, and **chrome125–130 are also invalid targets in this build** (only 99/100/101/104/107/110/116/119/120/123/124/131/133a/136/142/145/146 exist). Rotation list trimmed to valid targets only.
  2. Firefox/Edge fallback was dead code — TLS_PROFILES defined firefox124/125 and edge124/131 which map to unsupported targets. Profiles realigned to supported versions: `firefox135_win`/`firefox133_win`, `edge101_win`/`edge99_win`.
  3. `_profile_candidates` now iterates **every** non-deprecated profile per family (weight-sorted) and filters by `_KNOWN_TARGETS`, so a family isn't skipped because its top-weighted profile maps to an unsupported target.
  4. Double `record_failure` fixed — the fallback loop only records when the inner fetch path didn't (`status_code == 0` = tarpit/exception); 403/5xx are already recorded inside `_fetch_via_*`.
  5. `js_challenge_solver` checks hard block markers (`google.com/sorry`, `/sp/captcha`, `challenge-platform`, `cf-challenge`, `blocked by network security`) BEFORE the link-density bail, so link-rich `/sorry/`/Cloudflare interstitials are still caught.

### Test Results (evening, default DecisionEngine pipeline, bypass_cache=true)
- **Host — REDDIT** `https://www.reddit.com/r/technology/`: ✅ `success=True status=200 strat=drissionpage_plus ab=15 q=100 html=88558B` (real content)
- **Docker container — REDDIT**: ✅ `success=True strat=drissionpage_plus ab=15 q=100 html=88558B` (2nd request 9.3s warm) — **container now identical to host**
- **Google** `?q=weather+in+London`: ❌ Google serves `/sorry/` 429 to every strategy **including real browsers** — IP soft-flag (from today's high test volume; same for host and container). Not a code/fingerprint issue; clears with cooldown or residential proxy.
- **DuckDuckGo**: ✅ via `simple`, 20KB html + 8.9KB markdown
- **HTTPS CONNECT tunnel** (port 8888): raw TCP passthrough works (91KB) — no content processing by design.

### SF-14 🔴 Brave 1.93 cookie encryption reverse-engineered — v10 sync-compat (NOT the keyring portal secret)
- **Goal:** Import the user's real Brave session cookies (Google/Reddit) into the container's GUI Chrome so blocked sites trust it without captchas.
- **Wrong paths (all eliminated with proof):**
  1. **Portal keyring secret is a red herring.** Brave 1.93.129 (Chromium ~149) stores `os_crypt.portal` (dict with only `prev_desktop`/`prev_init_success`) and a 64-byte `Application key for brave_brave` keyring item. HKDF derivations (salt=`fdo_portal_secret_salt`, info=`HKDF-SHA-256 AES-256-GCM` — the exact pre-June-2026 Chromium values from `secret_portal_key_provider.cc`@`625faa7^`) do NOT decrypt cookies. The portal provider tags its data **`v12`**; our cookie DB has **1599/1599 `v10`** rows → the portal provider never encrypted the cookies.
  2. **FreedesktopSecretKeyProvider** (`v11` tag, AES-128-CBC, libsecret password) — no `Chromium Safe Storage` entry exists for brave in the keyring (only discord/Cursor/Code/chromium/obsidian/Claude). Not it.
  3. **Classic sync OSCrypt with `saltysalt`/PBKDF2 + libsecret** — tried raw/base64/text forms of all 16 keyring secrets × iterations 1/1003 × dkLen 16/32 × CBC; no match.
- **The actual scheme:** Chromium's `PosixKeyProvider` (`components/os_crypt/async/browser/posix_key_provider.cc`, present until the June 16 2026 `Remove kEncryptSyncCompat option` commit `83a7d11`). Because Brave 1.93 predates that removal, the cookie store (`CookieOSCryptoDelegate`) calls `GetInstance(..., Option::kEncryptSyncCompat)` → cookies are encrypted with the **hardcoded** `PosixKeyProvider` key:
  ```cpp
  constexpr char kEncryptionTag[] = "v10";
  // PBKDF2-HMAC-SHA1(1 iteration, key = "peanuts", salt = "saltysalt")
  constexpr auto kV10Key = {0xfd,0x62,0x1f,0xe5,0xa2,0xb4,0x02,0x53,
                            0x9d,0xfa,0x14,0x7c,0xa9,0x27,0x27,0x78};  // AES-128
  ```
  **This key is the same on every Linux Chromium/Brave install — no keyring involved.** That's why no keyring entry exists.
- **Blob layout (forensically verified byte-by-byte):**
  ```
  blob = "v10"(3) + salt(16) + IV(16) + AES-128-CBC(plaintext, PKCS7)
  key  = fd621fe5a2b402539dfa147ca9272778
  IV   = blob[19:35]   (random per cookie)
  salt = blob[3:19]    (random per cookie, ignore for decrypt)
  ```
  Evidence: `len(blob) % 16 == 3` for ALL 1599 cookies (`3+16+16+16n`). The first 16 bytes after `v10` are random (0xeb, 0x73, 0xb6, 0xee, 0xc6… across cookies) → NOT a version byte. Decrypting with the wrong IV produces valid-looking PKCS7 but garbage-prefixed plaintext (that's why earlier "1601/1601" attempts falsely passed — CBC only corrupts block 1 with a wrong IV). With `iv=blob[19:35]`, `ct=blob[35:]`: HSID = `AGme_wPcwfHCSJDpi` — perfectly clean.
- **Result:** **1601 v10 blobs → 1590 cleanly decrypted** (the 11 rejects are binary/non-UTF8 junk). 91 Google + 12 Reddit cookies (HSID/SSID/NID/SAPISID/SIDCC/OTZ/AEC/SEARCH_SAMESITE/__Secure-ENID, reddit `token_v2`/`reddit_session`/`lscache`/…) exported to Netscape format.
- **Deliverable:** `proxy-orchestrator/scripts/brave_cookies.py` — `extract` (decrypt DB → Netscape file) and `inject` (CDP `add_cookies` into GUI Chrome; `__Host-*` cookies handled as host-only via `url=` per Playwright's rule that url and path/domain are mutually exclusive). Dynamic column introspection (host Brave 1.93 schema lacks `is_same_party`).
- **GUI Chrome auto-inject:** `scripts/gui_browser.sh` now injects `/app/gui-cookies.txt` (or `$GUI_COOKIE_FILE`) into the persistent profile after launch, gated by marker `/app/.cookies-injected` (idempotent; runs even when Chrome was already up).
- **Verified in container:** 103/103 cookies injected (97 first pass + 6 `__Host-` via host-only fix) → survive `docker restart` (93 google + 12 reddit). Google search via `gui_chrome` through `/fetch`: **1.7–2.1s, status=200, ~150KB html, ~9KB markdown, 788–1108 words, 8–10 `<h3>` results, `/sorry` absent** — real SERPs incl. weather block + result cards. **Status:** ✅ FIXED (cookie import + GUI-stealth path live)

### SF-15 🔴 Cookie import baked into the whole system — real sessions power HTTP strategies (not just GUI VM)
- **Goal:** open-source the toolkit — cookie extraction + injection must be a first-class, GitHub-ready feature; GUI VM stays the *last* fallback (it's expensive); agents need an API knob to force a browser.
- **CookieManager upgrades** (`services/cookie_jar.py`):
  1. `import_netscape_file(path)` — bulk-import a Netscape cookie file (from `scripts/brave_cookies.py`) into the central jar.
  2. **Suffix domain matching** — `get_cookies()`/`export_as_header()` now merge cookies from the request domain AND all parent domains (`.google.com` cookies now apply to `www.google.com`; previously exact-match only → the imported cookies were invisible to HTTP strategies).
  3. `status()` for the API.
- **DecisionEngine** (`engine/decision_engine.py`):
  1. Loads `config.COOKIE_FILE` at startup into the jar → **every** strategy gets the real cookies: `simple` (reads `export_as_header`), Lib++ bridge (already synced), `gui_chrome` (seeds from jar).
  2. `_cookie_refresh_loop()` background task — re-checks the file mtime every `COOKIE_REFRESH_INTERVAL` (default 6h) and re-imports on change (drop a fresh export, no restart).
  3. `refresh_cookies()`/`cookie_status()` public API.
- **REST API** (`gateway/rest_api.py`):
  1. `use_browser: bool` + `browser: str` on `/fetch` — agents can force a real browser (picks gui_chrome → playwright → puppeteer → nodriver by availability).
  2. `GET /cookies/status` + `POST /cookies/refresh`.
- **Config:** `COOKIE_FILE` (default `/app/gui-cookies.txt`), `COOKIE_REFRESH_INTERVAL` (21600).
- **Verified in container:** `POST /cookies/refresh` → `imported:103, domains:29`. Cookie headers confirmed flowing: reddit header contains `token_v2`+`reddit_session`, google header contains `NID`+`HSID`. **Google via `simple` (plain HTTP, cookies only): 6.9–7.9s, 200, 561–743 words, 10 `<h3>` real results, no captcha.** old.reddit via `curl_cffi_plus`: 2.3–25.4s, 200, 876–1013 words. GUI Chrome stays LAST in STRATEGY_ORDER (`…,puppeteer_plus,gui_chrome`), never promoted by JS-heavy reorder (not in `js_capable_strategies`). **Status: ✅ FIXED**
- **Deliverables:** `scripts/brave_cookies.py` (extract/inject), `scripts/gui_browser.sh` (auto cookie-inject on boot, marker-gated), `docs/banner.svg` (code-style SVG repo banner), full GitHub-ready `README.md` (badges, 11-tier table, cookie guide, agent API docs, troubleshooting).

### SF-16 🔴 Fingerprint INCONSISTENCY flagged both paths — pinned ONE stealth persona across ALL strategies + auto cookie refresh
- **Symptom (user report):** "previously browser + cookie worked, now CLI + cookie and browser fails… different cmds do different things… inconsistency makes Google's alert and fool both."
- **Root cause — every path presented a DIFFERENT browser identity while carrying the same real cookies:**
  1. **`simple`** rotated TLS families from `CURL_CFFI_IMPERSONATE` (chrome131/chrome124/safari18_0/safari17_0/chrome146/chrome142/chrome136/edge101/firefox133), picked a **random UA** from `ua_pool` (50+ agents), a **random Accept-Language**, and a **hardcoded Chrome/120 Sec-CH-UA** — so one request looked like Chrome 131 and the next like Safari 18, Firefox 133, Edge… all from the same IP with the user's real Google/Reddit cookies.
  2. **`curl_cffi_plus`/`tls_rotator`/`drissionpage_plus`** independently rotated TLS profiles, random Accept-Language, random `Viewport-Width` per request, and hardcoded Chrome/125 UA fallbacks in `nodriver`/`puppeteer_plus`/`drissionpage` pools.
  3. **GUI Chrome** presented a DIFFERENT fixed identity — CDP reported `Chrome/149.0.7827.55` with UA `Chrome/125.0.0.0` (Playwright build default) while the HTTP path sent `Chrome/146` etc.
  4. **The cookie jar disconnect:** `curl_cffi_plus`/`tls_rotator`/`drissionpage` read cookies from Lib++'s `cross_strategy_jar`, which was **never populated from the central `CookieManager`** (bridge only pushed Lib++→external, never pulled). So the first strategy in the pipeline sent **ZERO cookies** while `simple` sent them all — another identity mismatch.
- **Impact:** Google's risk engine sees one IP + the user's real session cookies coming from 9 different browsers with different headers → flags BOTH the HTTP path AND the GUI Chrome path (correlated account/IP risk). "Works in browser, fails in CLI, vice versa" is exactly this.
- **Fix — single persona (default ON):**
  1. `config.py`: `PERSONA_PINNED=true`, `PERSONA_TLS=chrome146`, `PERSONA_UA` (Chrome/146 Windows), `PERSONA_ACCEPT_LANGUAGE=en-US,en;q=0.9`, `PERSONA_SEC_CH_UA`/`PERSONA_PLATFORM`/`PERSONA_VIEWPORT`.
  2. `simple.py`: pinned → `_IMPRESONATE_TARGETS=[chrome146]` (no rotation), persona UA (no `ua_pool` random), persona Accept-Language (no random), persona Sec-CH-UA/Platform. **Google flow reordered persona-first; legacy IE11 UA trick only when `PERSONA_PINNED=false`** (IE11 from the same IP as Chrome was a second identity).
  3. Lib++ `tls_profiles.py`: new `chrome146_win` profile (weight 10) + `persona_pinned()`/`persona_profile_name()`; `select_profile()` returns the persona profile when pinned (no weighted-random, no 5-min rotation).
  4. `curl_cffi_plus.py`: `_profile_candidates()` returns only `[persona]` when pinned; `CanvasSpoofer.get_spoofed_headers()` pins Accept-Language/Viewport-Width/Sec-CH-UA; `_UA_TEMPLATES["chrome"]` = persona UA.
  5. `tls_rotator.py` + `drissionpage_plus.py`: no 20% random exploration when pinned; persona UA + fixed Accept-Language; DrissionPage pool default UA = persona.
  6. `nodriver_strategy.py` + `puppeteer_plus.py`: hardcoded Chrome/125 UA → persona UA (env-aware).
  7. `gui_browser.sh`: GUI Chrome launched with `--user-agent="$PERSONA_UA"` so the browser path presents the SAME UA as every HTTP strategy.
  8. `session_cookie_sharing.py`: `get_cookies()` now **lazy-pulls from the external CookieManager** when the local jar is empty — the imported Netscape cookies now actually reach curl_cffi_plus/tls_rotator/drissionpage (previously they silently never left the central jar).
- **Auto cookie refresh:**
  1. `_cookie_refresh_loop` interval default 21600→**1800** (30 min); initial mtime recorded at startup so the loop only re-imports on change.
  2. `refresh_cookies()` now ALSO runs `brave_cookies.py inject` into the GUI Chrome (`gui_chrome_injected` in the API response) — both paths always carry the same cookie set.
  3. `gui_browser.sh` injection is now **mtime-aware**: re-injects whenever the cookie file is newer than the last injection marker (previously marker-gated only → stale cookies on boot after a refresh).
- **Banner fix:** `docs/banner.svg` lines 2–3 had hardcoded `x` positions that OVERLAPPED (e.g. `DecisionEngine(` at x=178 spans ~143px but the next token started at x=290; the closing `)` sat inside the URL string). Rewrote the code lines with **`<tspan>` flow** so characters never collide; verified XML-valid.
- **Verified in container (persona pinned):**
  ```
  GUI Chrome UA after boot:  Chrome/146.0.0.0 (matches persona)
  POST /cookies/refresh → {"imported":103, "gui_chrome_injected":true, "domains":29, "total_cookies":103}
  GOOGLE   (simple)        2.0s 200  740 words  10 <h3>  no /sorry   tls=chrome146
  GOOGLE   (gui_chrome)    1.7s 200  883 words  7  <h3>  no captcha  ua=Chrome/146
  OLD-REDDIT (curl_cffi_plus) 1.9s 200  879 words  real posts  tls=chrome146_win
  cross_strategy_jar → reddit len=2623 token_v2=True google NID/HSID present
  ```
- **Status:** ✅ FIXED (persona pinning + cookie bridge lazy-pull + auto refresh + GUI sync + banner)

## Notes / non-bugs (audited & cleared)
- `FetchRequest`/`FetchResult` signature usages consistent across `proxy_server`, `rest_api`, `websocket_api`, `decision_engine`.
- `request_dedup` future cancellation guarded by `done()`.
- `proxy_server` `_pipe`/`_IP` cache safe (single-threaded event loop, no awaits in mutations).
- `curl_cffi` impersonation targets accepted at runtime by curl_cffi 0.15.0.
- `aioquic` `create_protocol` positionic signature matches.
- `api_service` central `_do_solve` try/except already guards image/math type errors.
- No hardcoded secrets in `Lib++`/`proxy-orchestrator` package code (the one real secret is BYP-3).

## Test Results — proxy-orchestrator (host, non-Docker)
- **Scrapling fix applied:** `chrome_version`/`chromium_version` downgraded 149→131 in browserforge fingerprints.
- **Google** (`https://www.google.com/search?q=weather+in+London`):
  - REST API (`/fetch`): Returns Google "trouble accessing" block page via `tls_rotator` (now fixed - PO-14 resolved, brotli decompression working)
  - HTTP proxy mode (`curl -x http://localhost:8888 ...`): **VERIFIED WORKING** — returns 142KB HTML with 10 Google search results. Markdown extraction via `html_to_markdown()` produces clean results.
  - Google block page detection: `_detect_block_page()` now correctly identifies Google "trouble accessing" pages (including Bengali-language variants) via `yvlrue` container ID and other patterns.
- **Reddit** (`https://www.reddit.com/r/technology/`):
  - HTTP proxy mode: Returns 200 with Reddit's "Please wait for verification" JS PoW challenge page (8437 bytes). Block page detection correctly identifies this via `_detect_block_page()`.
  - REST API `/fetch`: Pipeline escalates through all strategies; `drissionpage_plus` hangs due to Reddit's anti-bot.
  - Reddit's JS PoW (seed-doubling algorithm) requires a real browser engine to solve. The `js_challenge_solver.py` routes through Playwright to solve it, but the hardcoded port 8085 bug prevented this from working (now fixed).
  - Reddit blocks all non-browser requests (HTTP proxy, curl, simple strategy) with 403 or connection failures.
  - The `drissionpage_plus` strategy has Reddit in its `js_domains` list and uses ChromiumPage with smart element waiting for `shreddit-app` and `div[data-testid]`.
- **Markdown extraction**: Now baked into REST API `/fetch` endpoint. Returns both `html` and `markdown` + `markdown_metadata` fields. Works for Google, Wikipedia, generic pages, and correctly detects block/captcha pages.
  - **HTTP Proxy Mode** (`curl -x http://localhost:8888 http://www.google.com/search?q=weather+in+London`): **VERIFIED WORKING** — Returns 142KB HTML with 10 Google search results. `clean_dom()` + `make_google_results_markdown()` produces clean markdown.
  - **Bug found:** Google returns "trouble accessing" block page via REST API. Our code didn't detect this as a challenge.
  - **Fix applied:** Added Google block detection patterns to `js_challenge_solver.py` and `captcha_solver_bridge.py`, added Playwright re-fetch bypass in `decision_engine.py`, and fixed quality scorer to reject challenge pages.
- **Reddit** (`https://www.reddit.com/r/technology/top.json?limit=3`):
  - Strategy: `drissionpage_plus`
  - Status: 200, HTML len 88558, antibot `ok`, quality usable `True`
- **Puppeteer/Puppeteer+ strategies** still fail on host (missing global npm `puppeteer-extra` module).

### Markdown Extraction (Verified Working via HTTP Proxy Mode)
- Feature: `make_google_results_markdown()` in `engine/dom_cleaner.py` formats Google search results as clean markdown.
- Triggered automatically when `_extract_google_results()` finds Google SERP result blocks in cleaned HTML.
- Output stored in `result.metadata["google_markdown"]`.
- **Verified working**: HTTP proxy mode (`curl -x http://localhost:8888 http://www.google.com/search?q=weather+in+London`) returns 142KB of HTML with 10 Google search results. `clean_dom()` + `make_google_results_markdown()` produces clean markdown with linked titles and snippets.
- **Full page markdown**: `markdownify()` on cleaned HTML produces 53KB of readable content including navigation, search box, and all result cards.
- **Reddit markdown extraction test was invalid** — Reddit does not contain Google-style search result elements. Markdown extraction is Google-specific.

### Test API (port 8080)
- `GET /health` — works
- `POST /fetch` — works with markdown extraction (Google block page detected, Reddit PoW challenge detected)
- `GET /stats` — works (per-domain stats)
- `GET /stats/cache` — works (L1/L2 cache stats)
- `GET /stats/proxies` — works (empty, no proxies configured)
- `GET /stats/circuits` — works (connection circuit states)
- `GET /metrics` — works (Prometheus format)
- `GET /ril/stats` — returns "RIL not available" (expected, RIL not configured)
- `WS /ws/fetch` — works (real-time streaming fetch)

### Proxy Server (port 8888) — TCP Tunnel Duo (HTTP + HTTPS)
- **HTTP proxy mode** (non-CONNECT): Routes through DecisionEngine pipeline with anti-bot bypass, DOM cleaning, and Google markdown extraction. **WORKING.**
- **HTTPS CONNECT mode**: Raw TCP tunnel (Happy Eyeballs v2). No content processing.
- `curl -x http://localhost:8888 http://www.google.com/search?q=weather+in+London` — **works!** Returns 142KB of HTML with 10 Google search results. Markdown conversion produces clean results.
- `curl -x http://localhost:8888 https://en.wikipedia.org/wiki/London` — works (1862478 bytes)
- `curl -x http://localhost:8888 https://www.reddit.com/r/technology/top.json?limit=3` — returns 403 (Reddit blocks proxy requests)
- `curl -x http://localhost:8888 https://www.reddit.com/r/technology/` — returns 200 with Reddit's "Please wait for verification" JS PoW challenge page (8437 bytes)

### HTML to Markdown Conversion (Verified Working)
- **Google HTML → Markdown**: Verified working via HTTP proxy mode
  - `clean_dom()` extracts 10 Google search results from the HTML
  - `make_google_results_markdown()` formats them as clean markdown with linked titles and snippets
  - Full page markdown via `markdownify()` produces 53KB of readable content
- **Wikipedia HTML → Markdown**: Verified working (486KB of readable markdown)
- **Reddit HTML → Markdown**: Reddit's JS PoW challenge page is detected as blocked by `_detect_block_page()` and returns a meaningful error message instead of empty markdown
- The `make_google_results_markdown()` function in `dom_cleaner.py` formats Google search results as clean markdown with linked titles and indented snippets
- WebSocket `/ws/fetch` — works (start event received, Reddit fetch times out due to Reddit's anti-bot)

### dom_to_markdown.py Fixes (Lib++/processors/dom_to_markdown.py)

#### Bug: Google domain detection broken for `www.google.com` (FIXED)
- The `_detect_site()` method stripped `www.` from the domain before matching, causing `google.com` to not match the `"google."` pattern (since `google.com`.endswith(`.google.`) is `False`).
- Fix: Changed matching logic to use `pattern.rstrip(".") in domain` for prefix patterns like `"google."`, and proper suffix matching for exact patterns like `"reddit.com"`.

#### Bug: No captcha/block page detection in markdown extraction (FIXED)
- When HTML was a captcha or block page (Google "trouble accessing", Reddit "Please wait for verification"), the markdown extraction returned empty content with `success=False` but no indication of why.
- Fix: Added `_BLOCK_PAGE_INDICATORS` list with 30+ patterns including English, Bengali, and other language variants. Added `_detect_block_page()` function that scans HTML before extraction and returns a meaningful error. Added `_detect_captcha()` and `_is_google_block_page()` helper functions.
- Added `yvlrue` (Google's block page container ID) to block page indicators.
- Added `please wait for verification` (Reddit's JS PoW challenge) to block page indicators.

#### Bug: Inconsistent `_SITE_HANDLERS` matching (FIXED)
- `"google."` used prefix matching while `"reddit.com"` and `"wikipedia.org"` used exact matching. The `_detect_site()` method was inconsistent.
- Fix: Unified matching logic with proper suffix-based detection for all patterns.

### js_challenge_solver.py Fix (Hardcoded Port 8085)

#### Bug: Hardcoded port 8085 instead of `config.API_PORT` (FIXED)
- `js_challenge_solver.py` line 134 hardcoded `http://localhost:8085/fetch` for Playwright re-fetch, but `config.py` sets `API_PORT` default to `8080`. This broke all PoW solving in default deployments.
- Fix: Changed to `f"http://localhost:{config.API_PORT}/fetch"` with proper import of `config`.

### REST API Markdown Integration

#### Feature: Markdown extraction baked into `/fetch` endpoint (NEW)
- Added `html_to_markdown()` import from `Lib_plus_plus.processors.dom_to_markdown` to `rest_api.py`.
- Added `markdown` and `markdown_metadata` fields to `FetchResponseBody` model.
- The `/fetch` endpoint now calls `html_to_markdown()` after `engine.fetch()` and includes the result in the response.
- `markdown_metadata` includes: `word_count`, `extraction_method`, `title`, `success`.

### Reddit Test Results

- Reddit via HTTP proxy (port 8888): Returns 200 with Reddit's "Please wait for verification" JS PoW challenge page (8437 bytes for HTML page).
- Reddit via REST API `/fetch`: Pipeline escalates through all strategies; `drissionpage_plus` hangs due to Reddit's anti-bot.
- Reddit PoW challenge detected by `_detect_block_page()` as `please wait for verification` — correctly identified as blocked.
- Reddit's JS PoW (seed-doubling algorithm) requires a real browser engine to solve. The `js_challenge_solver.py` routes through Playwright to solve it, but the hardcoded port 8085 bug prevented this from working.
- The `drissionpage_plus` strategy has Reddit in its `js_domains` list and uses ChromiumPage with smart element waiting for `shreddit-app` and `div[data-testid]`.
- Reddit blocks all non-browser requests (HTTP proxy, curl, simple strategy) with 403 or connection failures.
- The Reddit API (`/r/technology/top.json`) requires proper headers and may be more accessible than the HTML frontend.