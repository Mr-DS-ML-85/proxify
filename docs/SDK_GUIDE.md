# 🛠 Proxify SDK Guide

This guide covers advanced usage patterns for the official Python and Node.js SDKs, focusing on production-grade resilience and stateful session management.

---

## ⚡ 1. High-Performance Concurrency (Python)

Leverage `asyncio` to handle massive scraping workloads with automated bypass.

```python
import asyncio
from proxy_orchestrator import OrchestratorClient

async def scrape_google(query):
    client = OrchestratorClient()
    try:
        # Intelligent rate limiting and rotation handles Google.com blocks natively
        result = await client.fetch_async(
            url=f"https://google.com/search?q={query}",
            bypass_cache=True,
            strategy="scrapling"
        )
        return len(result['html'])
    except Exception as e:
        print(f"❌ Failed to scrape {query}: {e}")
        return 0

async def main():
    queries = ["python", "golang", "rust", "zig", "mojo"]
    tasks = [scrape_google(q) for q in queries]
    
    # Executed in parallel via the Decision Engine
    results = await asyncio.gather(*tasks)
    print(f"✅ Total bytes fetched: {sum(results)}")

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 🍪 2. Stateful Session Persistence

Maintain a consistent identity (Cookies, UA, JA3) across multiple requests. This is critical for logged-in states or multi-step form submissions.

### Sticky Sessions (Python)
```python
client = OrchestratorClient()

# Initialize session
client.fetch("https://target.com/login", sessionId="user_1")

# The next request will automatically share the same cookies and fingerprint
result = client.fetch("https://target.com/dashboard", sessionId="user_1")
```

### Forced Session Rotation
If a session is flagged or you want to rotate fingerprints on demand:
```python
# Clears the existing session data and generates a new UA/CookieJar
client.fetch("https://target.com/refresh", sessionId="user_1", forceNewSession=True)
```

---

## 📡 3. Real-Time Fetching (WebSockets)

Use the WebSocket clients to reduce HTTP handshake overhead for high-frequency requests.

### Python Example
```python
from proxy_orchestrator import AsyncOrchestratorWSClient

async def ws_example():
    ws = AsyncOrchestratorWSClient()
    await ws.connect()
    
    # WebSocket results arrive as JSON messages
    result = await ws.fetch("https://example.com", options={"sessionId": "ws-bot-1"})
    print(f"Fetched via WS: {result['strategy_used']}")
    
    await ws.close()
```

### Node.js Example
```javascript
const { OrchestratorWSClient } = require('./ws-client');

async function wsExample() {
    const ws = new OrchestratorWSClient();
    await ws.connect();
    
    const result = await ws.fetch('https://example.com', 'GET', { sessionId: 'ws-bot-2' });
    console.log(`Bypass successful: ${result.success}`);
    
    ws.close();
}
```

---

## 📊 4. Telemetry & Analytics

Access internal engine metrics to optimize your scraping performance.

```python
stats = client.get_stats()

# Check cache health
print(f"L1 Cache Hit Rate: {stats['cache']['l1']['hits'] / stats['cache']['l1']['requests'] * 100:.2f}%")

# Check upstream proxy health
for proxy, data in stats['proxies'].items():
    print(f"Proxy {proxy}: {data['status']} | Latency: {data['avg_latency']}s")
```

---

## 🛑 5. Exception Handling

Always wrap your calls in try-except blocks to handle network or strategic failures gracefully.

```python
from proxy_orchestrator import BypassFailedError, RateLimitError

try:
    client.fetch("https://hard-to-scrape.com")
except BypassFailedError:
    # Occurs when all strategies (Simple, Scrapling, FlareSolverr) were blocked
    print("❌ All bypass attempts failed.")
except RateLimitError:
    # Occurs when you exceed the configured internal limits
    print("⚠️ Rate limit reached.")
```

---

> [!TIP]
> For standard libraries that don't support custom APIs, use `client.get_proxy_client()` to get a pre-configured `httpx` or `axios` instance.
