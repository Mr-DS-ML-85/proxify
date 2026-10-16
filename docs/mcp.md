# Proxify MCP — Browser Control for AI Agents

`proxify-mcp` exposes Proxify's browser interaction layer over the Model Context
Protocol, so an agent can drive a real browser the way a person does: look at the
page, choose a target by what it *is*, act on it, and read the result.

Works with any MCP client — Codex GUI, Claude Cowork, Claude Desktop, Hermes,
OpenCode, Cursor, Grok-backed agents, and anything else speaking MCP.

---

## Why element-first

Most "AI browser" tools take a screenshot and click coordinates. That is slow,
expensive, and brittle: the same page yields different pixels at different
window sizes, and the model has to re-read the screen after every click.

Proxify's agents read the **accessibility tree** instead — the same structured
snapshot a screen reader builds: every interactive element with its role,
accessible name, and state. The model picks a target by meaning:

```
role="button", name="Submit order"
```

and acts on that element directly. No coordinates are guessed, and the action
survives reflow, resizing, and re-rendering.

Mouse and keyboard input are still available for the cases where an element
does not exist — canvas surfaces, native menus, drag paths, and hover states.

---

## Install

```bash
cd proxy-orchestrator
pip install 'mcp>=1.2' playwright
playwright install chromium          # or set CHROMIUM_PATH to a system Chrome
```

## Run

```bash
# stdio — for local MCP clients
python proxify_mcp.py

# streamable HTTP — for shared or remote use
python proxify_mcp.py --http --port 8090
```

## Client configuration

**stdio** (Claude Desktop, Cursor, Codex, OpenCode, Hermes):

```json
{
  "mcpServers": {
    "proxify": {
      "command": "python3",
      "args": ["/path/to/proxify/proxy-orchestrator/proxify_mcp.py"],
      "env": {
        "PROXIFY_REST_URL": "http://127.0.0.1:8081",
        "PROXIFY_TZ": "Asia/Dhaka",
        "CHROMIUM_PATH": "/usr/bin/chromium-browser"
      }
    }
  }
}
```

**streamable HTTP** (containers, remote agents):

```json
{
  "mcpServers": {
    "proxify": { "url": "http://127.0.0.1:8090/mcp" }
  }
}
```

| Env var | Default | Purpose |
|---|---|---|
| `PROXIFY_REST_URL` | `http://127.0.0.1:8081` | Orchestrator REST API used by `fetch_url` |
| `PROXIFY_MCP_HEADLESS` | `1` | Set `0` to watch the browser work |
| `PROXIFY_TZ` | `Asia/Dhaka` | Timezone pinned to match the egress IP |
| `CHROMIUM_PATH` | Playwright's | System browser if Playwright's own is absent |

---

## Tools

### Navigation and perception

| Tool | Purpose |
|---|---|
| `browser_open(url, wait_for_hydration=true, timeout=30)` | Open a page. Returns title and URL. Hydration is awaited by default so a single-page app is never read half-rendered. |
| `browser_snapshot(limit=120)` | **The agent's eyes.** Interactive elements with role, accessible name, tag, disabled and focused state. |
| `browser_wait_for_hydration(timeout=20)` | Block until a client-rendered app has mounted real content. |
| `page_text()` | Readable text with navigation, aside and footer stripped. Flags unhydrated pages instead of returning a skeleton. |

### Acting

| Tool | Purpose |
|---|---|
| `browser_click(role, name, text, selector, x, y, button, clicks, nth)` | Click by role+name (preferred), text, selector, or coordinates. `clicks=2`/`3` for double/triple. |
| `browser_type(text, role, name, selector, nth, delay_ms)` | Focus a field and type key-by-key so input handlers fire. |
| `browser_press(key, hold_ms)` | Keys and combos: `Enter`, `ctrl+a`, `PageDown`. `hold_ms` holds a modifier. |
| `browser_scroll(delta_y, delta_x, selector)` | Scroll the page or a specific container. |
| `browser_hover(role, name, text, selector)` | Reveal hover-only menus and tooltips. |
| `browser_drag(to_x, to_y, ...)` | Drag from an element or the viewport to a point. |
| `browser_wait(seconds)` | Fixed pause for animations or slow requests. |

### Observing

| Tool | Purpose |
|---|---|
| `browser_screenshot(path, full_page)` | PNG capture. Use when layout or imagery actually matters, not for general reading. |

### Fetching

| Tool | Purpose |
|---|---|
| `fetch_url(url, as_markdown=true, timeout=60)` | Read a page through Proxify's tiered anti-bot pipeline. No browser, no interaction. |

---

## The right tool for the job

| Need | Use |
|---|---|
| Read an article, docs page, news | `fetch_url` — fast, no browser |
| Log in, search with filters, fill a form | `browser_*` |
| Work behind a JS challenge | `browser_open` with hydration wait |
| Understand what is on screen | `browser_snapshot` |
| Verify how something *looks* | `browser_screenshot` |

`fetch_url` costs a fraction of a browser round-trip. Reach for the browser
only when the task genuinely requires interaction.

---

## How clicks are delivered

A single dispatched event is trivially detectable as synthetic. `browser_click`
sends a realistic sequence: a move to the target, a press, a short delay, then a
release — matching what a physical mouse produces. When an element exists but
cannot be clicked normally (covered, detached, mid-animation), the action falls
back to the centre of its bounding box, and only then to raw coordinates. Each
result reports which path was used in `method`.

---

## Typical agent loop

```
browser_open(url)
  → browser_snapshot()
      read the elements, choose a target
  → browser_click(role="textbox", name="Search")
  → browser_type("reverse engineering", role="textbox", name="Search")
  → browser_press("Enter")
  → browser_wait_for_hydration()
  → browser_snapshot()          # results have replaced the form
  → browser_click(role="link", name="first result")
  → page_text()                 # read it
```

State persists between calls — one browser, one page, like a single window. The
agent does not reopen the page each turn.

---

## Anti-bot posture

The browser is configured to look like the machine it is rather than pretending
to be something else:

- **Persistent profile** — cookies and storage survive across runs, so a target
  that issued a clearance cookie does not have to re-issue it.
- **Coherent persona** — user-agent, client hints, locale, and timezone all
  agree. A UTC clock on a mismatched egress IP is a strong automation signal.
- **Hydration-aware** — waits for the app to mount before reading, so the agent
  never acts on an empty shell.
- **Escalation fallback** — when interaction is not required, `fetch_url` uses
  the orchestrator's tiered cascade instead of paying for a browser.

Residential proxies are **not required** for the large majority of targets.
The hardest anti-bot vendors — Cloudflare managed challenge, Turnstile, DataDome,
Akamai — weigh IP reputation alongside TLS and behavioural signals, so a
datacentre IP remains a genuine limitation on that small residue. For those,
point `fetch_url` at Proxify's proxy configuration and supply an egress.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `No page open` | Tool called before `browser_open` | Always open first |
| Snapshot is empty | Page still hydrating | `browser_wait_for_hydration()` |
| Click misses | Target not in the snapshot | Re-snapshot; check `disabled` |
| `Executable doesn't exist` | Playwright browser missing | `playwright install chromium` or set `CHROMIUM_PATH` |
| `fetch_url` errors | Orchestrator not running | Start it, check `PROXIFY_REST_URL` |
| Challenge page returned | Target needs a heavier tier | Use `browser_open`; add a residential egress for the hard residue |

---

## Tests

```bash
python3 engine/test_antibot_regression.py   # 3/3 — block scoring
python3 engine/test_spa_cleaner.py          # 3/3 — SPA extraction
CHROMIUM_PATH=/usr/bin/chromium-browser python3 engine/test_interaction.py   # 11/11
```