# 🔧 Troubleshooting & Diagnostic Tools

This guide addresses common issues and provides tools to verify your installation.

## 1. 🛡️ SSL Certificate Issues (MITM)

Because the Proxify uses **Dynamic MITM Interception**, your client must trust our Root CA.

### I deleted my `.certs` folder! What do I do?
**The Fix is Simple**: Restart the Proxify. 
The system detects missing certificates on startup and will automatically perform an atomic regeneration of the entire certificate chain (Root CA + Domain Leafs). No manual action is required!

### SSL: CERTIFICATE_VERIFY_FAILED
Found at: `.certs/proxy_orchestratorCA.pem`

*   **cURL**: Use `--proxy-insecure` or `-k`.
*   **Python (Requests)**:
    ```python
    requests.get("https://google.com", proxies={"https": "http://localhost:8888"}, verify=".certs/proxy_orchestratorCA.pem")
    ```

---

## 2. 🩺 Diagnostic Healthcheck Tools (`tests/`)

Use these scripts to verify specific components of the orchestrator.

| Tool Name | Purpose |
| :--- | :--- |
| `tool_test_mitm.py` | Verifies the Root CA can sign certificates. |
| `tool_test_proxy_fetch.py` | Basic HTTP/HTTPS proxy fetch test. |
| `tool_test_json.py` | Verifies the Chromium DOM Unwrapper. |
| `tool_test_api_errors.py` | Tests the fail-safe response normalizer. |
| `tool_test_ssl_bug.sh` | Stress-tests concurrent SSL handshakes. |

---

## 3. 🔍 SearXNG Specific Engine Failures (Google/Vimeo)

If SearXNG reports errors for specific engines (e.g., `Vimeo`, `Startpage`, or `Unsplash`) while using the proxy:

1.  **Check the Proxy Logs**: If the proxy shows `200 OK` but SearXNG fails, it means the **Proxify successfully delivered the unblocked HTML**, but SearXNG's internal scraper regex is outdated. 
2. ### ❌ ConnectionReset / MITM TLS Error (Startpage/SearXNG)
If you see `ConnectionResetError` or `BrokenPipeError` for specific domains like Startpage in the logs, it usually means the client (e.g. SearXNG) timed out while waiting for a complex bypass (browser-based) to finish.

**Fix**:
1.  **Increase SearXNG Timeout**: In your SearXNG `settings.yml`, increase the `request_timeout` to at least `20.0` or `30.0` seconds.
2.  **Use Upstream Proxies**: Search engines like Startpage are extremely IP-sensitive. If you are hitting captchas even with `scrapling` strategy, it means your current server IP is flagged. Add high-quality residential or mobile proxies to `.env`.

### ⚙️ .env Changes are Ignored?
If you recently updated `.env` but don't see the changes in the terminal banner:
- Ensure you have a literal `.env` file (not just `.env.example`).
- If using Docker Compose, run `docker-compose down && docker-compose up --build -d` to ensure the new environment is injected into the container.
 If you see the real HTML, the proxy is working perfectly.
3.  **Image Loading**: Some images might not load in SearXNG if they are hosted on a domain that isn't being proxied or if the browser's hot-linking protection is active.

---

## 4. ⏱️ Timeouts (`504 Gateway Timeout`)

Bypassing anti-bots takes time. **Google.com** scraping, for example, requires intelligent rate limiting to avoid detection. 

### The Fix: Increase SearXNG Timeouts
Always set `outgoing.request_timeout` to at least `20.0` or `30.0` in your `settings.yml` to allow the orchestrator enough time to solve challenges.
