# Deployment Guide

---

## Docker Compose (Recommended)

Deploy as part of the Superbolt stack from the root project:

```bash
docker compose up -d --build proxy-orchestrator redis flaresolverr
```

The proxy is available at `:8888` and the REST API at `:8085`.

### Standalone (Within proxy-orchestrator dir)

```bash
cd proxy-orchestrator
docker build -t proxy-orchestrator .
docker run -p 8888:8888 -p 8085:8085 \
  -e REDIS_URL=redis://host.docker.internal:6379/0 \
  -e API_PORT=8085 \
  proxy-orchestrator
```

---

## Configuration

All settings via environment variables. Key parameters:

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_PORT` | `8888` | HTTP CONNECT proxy |
| `API_PORT` | `8080` | REST/WS API (use `8085` in Docker to avoid conflicts) |
| `API_HOST` | `0.0.0.0` | API bind address |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis L2 cache |
| `REDIS_ENABLED` | `true` | Toggle Redis cache |
| `FLARESOLVERR_URL` | `http://localhost:8191/v1` | FlareSolverr endpoint |
| `FLARESOLVERR_ENABLED` | `true` | Toggle FlareSolverr |
| `CURL_CFFI_ENABLED` | `true` | Toggle curl_cffi |
| `CURL_CFFI_PLUS_ENABLED` | `true` | Toggle Lib++ curl_cffi_plus |
| `NODRIVER_ENABLED` | `true` | Toggle Lib++ nodriver |
| `TLS_ROTATOR_ENABLED` | `true` | Toggle Lib++ TLS rotator |
| `SCRAPLING_ENABLED` | `true` | Toggle Scrapling |
| `PLAYWRIGHT_ENABLED` | `true` | Toggle Playwright |
| `PUPPETEER_ENABLED` | `true` | Toggle Puppeteer |
| `DRISSIONPAGE_PLUS_ENABLED` | `true` | Toggle Lib++ DrissionPage+ |
| `PUPPETEER_PLUS_ENABLED` | `true` | Toggle Lib++ Puppeteer+ |
| `STRATEGY_ORDER` | 10-tier chain | Comma-separated strategy priority |
| `CACHE_ENABLED` | `true` | Toggle caching |
| `L1_CACHE_MAX_SIZE` | `10000` | RAM cache entry limit |
| `L1_CACHE_TTL` | `300` | L1 TTL in seconds |
| `L2_CACHE_TTL` | `3600` | Redis TTL in seconds |
| `GLOBAL_RATE_LIMIT` | `100` | Global req/sec |
| `PER_DOMAIN_RATE_LIMIT` | `10` | Per-domain req/sec |
| `UPSTREAM_PROXIES` | — | Comma-separated proxy URLs for backend rotation |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

---

## Manual (Host) Deployment

```bash
# Install dependencies
pip install -r requirements.txt

# Ensure Redis + FlareSolverr are running
docker compose up -d redis flaresolverr

# Start
python3 main.py
```

For Scrapling/Playwright on host:
```bash
sudo apt-get install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 libgbm1 libasound2
playwright install chromium
```
