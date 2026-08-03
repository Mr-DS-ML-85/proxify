# =============================================================================
# Lib++ Dockerfile — Next-Gen Anti-Bot Strategy Layer
# =============================================================================
# Two build stages:
#   1. dev — development with hot-reload
#   2. prod — production with all dependencies baked in
#
# Usage:
#   docker build -t libplus:latest .
#   docker run --rm -it --network unified-net libplus:latest
# =============================================================================

FROM python:3.11-slim AS base

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y \
    curl \
    wget \
    gnupg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# =============================================================================
# DEV STAGE
# =============================================================================
FROM base AS dev

# Install Node.js for Puppeteer+
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g puppeteer puppeteer-extra puppeteer-extra-plugin-stealth \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Install optional extras
RUN pip install --no-cache-dir \
    httpx-curl-cffi \
    aioquic \
    websockets \
    nodriver \
    2>/dev/null || true

COPY . /app/

ENV PYTHONPATH=/app
ENV STEALTH_BROWSER_URL=http://stealth-browser:8000
ENV PROXY_ORCHESTRATOR_URL=http://proxy-orchestrator:8080

EXPOSE 9000

CMD ["python", "-c", "import asyncio; from Lib++.strategies.nodriver_strategy import NodriverStrategy; s=NodriverStrategy(); asyncio.run(s.initialize()); print('Lib++ nodriver pool ready'); asyncio.get_event_loop().run_forever()"]

# =============================================================================
# PROD STAGE
# =============================================================================
FROM base AS prod

RUN pip install --no-cache-dir \
    httpx \
    httpx-curl-cffi \
    curl-cffi \
    aioquic \
    websockets \
    nodriver \
    uvicorn \
    fastapi

# Install Chromium for Puppeteer+
RUN apt-get update && apt-get install -y chromium \
    && rm -rf /var/lib/apt/lists/*

COPY . /app/

ENV PYTHONPATH=/app
ENV STEALTH_BROWSER_URL=http://stealth-browser:8000
ENV PROXY_ORCHESTRATOR_URL=http://proxy-orchestrator:8080
ENV LOG_LEVEL=INFO

EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:9000/health || exit 1

CMD ["python", "-c", "import asyncio; from Lib++.strategies.nodriver_strategy import NodriverStrategy; s=NodriverStrategy(); asyncio.run(s.initialize()); print('Lib++ nodriver pool ready'); asyncio.get_event_loop().run_forever()"]
