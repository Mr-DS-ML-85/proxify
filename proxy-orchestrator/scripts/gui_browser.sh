#!/bin/bash
# gui_browser.sh — Launch the persistent HEADFUL GUI Chrome (Xvfb + CDP :9222)
#
# Gives the orchestrator a real desktop browser that accumulates cookies,
# cache, localStorage and history in /app/gui-profile — the strongest trust
# signal for Google-class anti-bots. Idempotent: safe to re-run.
#
# STEALTH PERSONA: the GUI Chrome is launched with the SAME User-Agent as the
# HTTP strategies (PERSONA_UA). If the GUI browser presented a different UA
# than the CLI path, Google would see your real cookies from two different
# browsers on one IP — exactly the inconsistency that gets both flagged.
#
# Usage: bash scripts/gui_browser.sh   (inside the container)

set -u

PERSONA_UA="${PERSONA_UA:-Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36}"

CHROME="/ms-playwright/chromium-1228/chrome-linux64/chrome"
[ -x "$CHROME" ] || CHROME=$(find /ms-playwright -name chrome -path "*chrome-linux64*" -type f 2>/dev/null | head -1)
[ -n "$CHROME" ] && [ -x "$CHROME" ] || { echo "FATAL: no chromium found"; exit 1; }

PROFILE="/app/gui-profile"
mkdir -p "$PROFILE"

# 1. Xvfb virtual display (idempotent)
if ! xdpyinfo -display :99 >/dev/null 2>&1; then
    rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null
    Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +extension RANDR +render -dpi 96 >/tmp/xvfb.log 2>&1 &
    sleep 2
    echo "[gui_browser] Xvfb :99 started"
else
    echo "[gui_browser] Xvfb :99 already running"
fi

# 2. Headful Chrome with persistent profile + CDP (idempotent)
CHROME_UP=0
if curl -s -m 2 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
    CHROME_UP=1
    echo "[gui_browser] GUI Chrome already running on :9222"
fi

if [ "$CHROME_UP" = "1" ]; then
    # still continue below: cookie auto-inject must run on every boot
    :
else
    DISPLAY=:99 "$CHROME" \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-setuid-sandbox \
    --no-first-run \
    --no-default-browser-check \
    --disable-background-networking \
    --disable-component-update \
    --disable-sync \
    --disable-translate \
    --disable-extensions \
    --window-position=0,0 \
    --window-size=1920,1080 \
    --user-agent="$PERSONA_UA" \
    --user-data-dir="$PROFILE" \
    --remote-debugging-port=9222 \
    --lang=en-US \
    --force-webrtc-ip-handling-policy=disable_non_proxied_udp \
    about:blank >>/tmp/gui_chrome.log 2>&1 &

for i in $(seq 1 15); do
    if curl -s -m 2 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
        echo "[gui_browser] GUI Chrome up on :9222 (profile=$PROFILE)"
        break
    fi
    sleep 1
done
fi

# 3. Optional: inject the user's real session cookies (extracted from their
#    browser via scripts/brave_cookies.py) for stealth — cookies + cache make
#    Google/Reddit trust the GUI browser. MTIME-AWARE: re-injects whenever the
#    cookie file is NEWER than the last injection (auto cookie refresh on boot).
COOKIE_SRC="${GUI_COOKIE_FILE:-/app/gui-cookies.txt}"
MARKER=/app/.cookies-injected
if [ -f "$COOKIE_SRC" ]; then
    NEED_INJECT=1
    if [ -f "$MARKER" ]; then
        SRC_MTIME=$(stat -c %Y "$COOKIE_SRC" 2>/dev/null || echo 0)
        MARK_MTIME=$(stat -c %Y "$MARKER" 2>/dev/null || echo 0)
        if [ "$SRC_MTIME" -le "$MARK_MTIME" ]; then
            NEED_INJECT=0
            echo "[gui_browser] cookies up to date (injected from $(cat $MARKER))"
        else
            echo "[gui_browser] cookie file is newer than last injection — re-injecting ..."
        fi
    fi
    if [ "$NEED_INJECT" = "1" ]; then
        echo "[gui_browser] injecting session cookies from $COOKIE_SRC ..."
        if python3 /app/scripts/brave_cookies.py inject --src "$COOKIE_SRC" \
                --cdp http://127.0.0.1:9222 >/tmp/cookie_inject.log 2>&1; then
            touch "$MARKER"
            echo "$COOKIE_SRC" > "$MARKER"
            echo "[gui_browser] cookies injected OK (see /tmp/cookie_inject.log)"
        else
            echo "[gui_browser] WARN: cookie injection failed — see /tmp/cookie_inject.log"
        fi
    fi
else
    echo "[gui_browser] no cookie file at $COOKIE_SRC (skipping injection)"
fi

if curl -s -m 2 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
    exit 0
fi
echo "[gui_browser] WARN: Chrome did not answer on :9222 — see /tmp/gui_chrome.log"
exit 1
