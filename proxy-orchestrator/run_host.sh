#!/bin/bash
# =============================================================================
# Proxify — Host Mode Runner (port 9090, using uv)
# =============================================================================
# Run this from the proxy-orchestrator directory:
#   bash run_host.sh
#
# Prerequisites (already installed on this host):
#   - uv (https://github.com/astral-sh/uv)
#   - Redis server (running on localhost:6379)
#   - Xvfb :99 (for GUI Chrome strategy)
#   - Playwright Chromium (installed)
#   - Chromium browser (snap or system)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║         Proxify — Host Mode (port 9090)                     ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── 1. Activate uv environment ──────────────────────────────────────
if [ ! -d ".venv" ]; then
    echo "→ Creating uv virtual environment..."
    uv venv
fi

source .venv/bin/activate

# ── 2. Install/update dependencies ──────────────────────────────────
echo "→ Installing dependencies..."
uv pip install -r requirements.txt --quiet

# ── 3. Install Lib++ as editable package ────────────────────────────
echo "→ Installing Lib++ (next-gen strategy layer)..."
uv pip install -e ../Lib++ --quiet

# ── 4. Ensure Playwright browsers are installed ─────────────────────
echo "→ Checking Playwright Chromium..."
playwright install chromium 2>/dev/null || true

# ── 5. Copy host .env if .env doesn't exist ─────────────────────────
if [ ! -f ".env" ]; then
    echo "→ Creating .env from host template..."
    cp .env.host .env
fi

# ── 6. Validate Redis connection ────────────────────────────────────
echo "→ Checking Redis..."
if redis-cli ping 2>/dev/null | grep -q PONG; then
    echo "  ✓ Redis is running on localhost:6379"
else
    echo "  ⚠ Redis is NOT responding — starting it..."
    redis-server --daemonize yes
    sleep 1
    redis-cli ping
fi

# ── 7. Validate Xvfb (for GUI Chrome) ──────────────────────────────
echo "→ Checking Xvfb..."
if xdpyinfo -display :99 >/dev/null 2>&1; then
    echo "  ✓ Xvfb :99 is running"
else
    echo "  ⚠ Xvfb :99 is NOT running — starting it..."
    rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true
    Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +extension RANDR +render -dpi 96 &
    sleep 2
    echo "  ✓ Xvfb :99 started"
fi

# ── 8. Check for GUI cookies file ───────────────────────────────────
COOKIE_FILE=$(grep '^COOKIE_FILE=' .env 2>/dev/null | cut -d= -f2)
if [ -n "$COOKIE_FILE" ] && [ ! -f "$COOKIE_FILE" ]; then
    echo "  ℹ Cookie file not found: $COOKIE_FILE"
    echo "    (Optional: export real browser cookies for stealth mode)"
    echo "    See: proxy-orchestrator/scripts/brave_cookies.py"
fi

# ── 9. Export env vars from .env ────────────────────────────────────
set -a
source .env
set +a

echo ""
echo "  ── Starting Proxify ──"
echo "  API:    http://localhost:${API_PORT}"
echo "  Proxy:  http://localhost:${PROXY_PORT}"
echo "  Health: http://localhost:${API_PORT}/health"
echo ""

# ── 10. Launch ──────────────────────────────────────────────────────
exec python main.py
