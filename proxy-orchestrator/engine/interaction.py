"""
Browser Interaction — element-first control with pixel/keyboard fallback.

Design follows the browser-agent pattern rather than pure pixel automation: the
page is perceived through the accessibility tree, a target is resolved to a real
DOM node, and the action is dispatched on that node. Mouse and keyboard input
remain available for canvas surfaces, native menus, drag, and hover, where no
addressable element exists.

Two input paths are used deliberately:

  * Playwright element handles for DOM-addressable targets. They resolve to a
    node, so no coordinate is ever guessed and the action survives reflow.
  * CDP ``Input.dispatch*`` events for raw pointer/key work. Needed for canvas,
    drag paths and anything a handle cannot express.

Action names mirror the ``computer_20251124`` tool surface so a caller can hand
these straight to an agent loop:

    screenshot, mouse_move, left_click, right_click, middle_click,
    double_click, triple_click, left_mouse_down, left_mouse_up,
    type, key, scroll, hold_key, cursor_position, wait
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Interaction roles ────────────────────────────────────────────────────────

# Roles an agent is likely to target, mapped to Playwright's role selectors.
ROLE_ALIASES: dict[str, tuple[str, ...]] = {
    "button": ("button",),
    "link": ("link",),
    "textbox": ("textbox",),
    "checkbox": ("checkbox",),
    "radio": ("radio",),
    "combobox": ("combobox",),
    "searchbox": ("searchbox",),
    "heading": ("heading",),
    "listitem": ("listitem",),
    "tab": ("tab",),
    "menuitem": ("menuitem",),
    "option": ("option",),
    "image": ("img",),
}

# Keys that need a special Playwright mapping rather than a literal string.
_KEY_ALIASES = {
    "enter": "Enter",
    "return": "Enter",
    "tab": "Tab",
    "esc": "Escape",
    "escape": "Escape",
    "space": " ",
    "backspace": "Backspace",
    "delete": "Delete",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "pageup": "PageUp",
    "pagedown": "PageDown",
    "home": "Home",
    "end": "End",
    # Modifiers. Playwright rejects "ctrl"/"shift" outright; it wants the
    # X11-style names below.
    "ctrl": "Control",
    "control": "Control",
    "shift": "Shift",
    "alt": "Alt",
    "option": "Alt",
    "meta": "Meta",
    "cmd": "Meta",
    "super": "Meta",
    "win": "Meta",
}


@dataclass
class ElementInfo:
    """One addressable node from the accessibility snapshot."""

    role: str
    name: str
    index: int
    tag: str = ""
    disabled: bool = False
    focused: bool = False

    def label(self) -> str:
        return f"{self.role} {self.name!r}".strip()


@dataclass
class ActionResult:
    """Outcome of a single interaction."""

    ok: bool
    action: str
    detail: str = ""
    target: Optional[ElementInfo] = None
    method: str = "element"  # "element" | "pixel" | "keyboard"
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "detail": self.detail,
            "method": self.method,
            "target": self.target.label() if self.target else None,
            "error": self.error,
        }


class InteractionError(RuntimeError):
    """Raised when an interaction cannot be attempted at all."""


class PageInteractor:
    """Element-first interaction over a Playwright page.

    Wraps a page and exposes the computer-use action set. Every method returns
    an :class:`ActionResult` rather than raising on a miss, so an agent loop can
    branch on ``ok`` without try/except noise.
    """

    def __init__(self, page: Any, cdp: Any = None, default_timeout: int = 8000):
        self._page = page
        self._cdp = cdp
        self._timeout = default_timeout
        self._cursor: tuple[int, int] = (0, 0)

    # ── Perception ────────────────────────────────────────────────────────────

    async def snapshot(self, limit: int = 200) -> list[ElementInfo]:
        """Return the accessibility-style snapshot: role, name, state.

        This is the same shape a screen reader builds, and the same one browser
        agents consume. It is cheap (a few KB) compared with a screenshot and
        survives layout changes.
        """
        script = """
        (limit) => {
          // Implicit ARIA roles by tag. An <a href> is a link even though the
          // tag is "a", so matching literal tag names alone misses most of a
          // normal page.
          const IMPLICIT = {
            a: 'link', button: 'button', h1: 'heading', h2: 'heading',
            h3: 'heading', h4: 'heading', h5: 'heading', h6: 'heading',
            img: 'image', nav: 'navigation', form: 'form',
            textarea: 'textbox', select: 'combobox', option: 'option',
            li: 'listitem', td: 'cell', th: 'columnheader',
            label: 'text', summary: 'button', details: 'group'
          };
          const out = [];
          const seen = new Set();
          const walker = document.createTreeWalker(
            document.body || document.documentElement,
            NodeFilter.SHOW_ELEMENT
          );
          let node;
          while ((node = walker.nextNode()) && out.length < limit) {
            const tag = node.tagName.toLowerCase();
            let role = node.getAttribute('role');
            if (!role) {
              if (tag === 'input') {
                const t = (node.getAttribute('type') || 'text').toLowerCase();
                role = { search: 'searchbox', checkbox: 'checkbox',
                         radio: 'radio', submit: 'button',
                         button: 'button', email: 'textbox',
                         password: 'textbox', text: 'textbox' }[t] || 'textbox';
              } else {
                role = IMPLICIT[tag] || '';
              }
            }
            if (!role || role === 'text' || role === 'cell' ||
                role === 'columnheader' || role === 'group' ||
                role === 'form' || role === 'navigation') continue;
            if (seen.has(node)) continue;
            seen.add(node);
            const name = (
              node.getAttribute('aria-label') ||
              (node.tagName === 'INPUT'
                ? (node.value || node.placeholder || '')
                : (node.innerText || node.textContent || ''))
            ).trim().replace(/\\s+/g, ' ').slice(0, 120);
            if (!name) continue;
            out.push({
              role, name, tag,
              disabled: !!node.disabled ||
                        node.getAttribute('aria-disabled') === 'true',
              focused: node === document.activeElement
            });
          }
          return out;
        }
        """

        try:
            rows = await self._page.evaluate(script, limit)
        except Exception as e:
            raise InteractionError(f"snapshot failed: {e}") from e

        return [
            ElementInfo(
                role=r.get("role", ""), name=r.get("name", ""),
                index=i, tag=r.get("tag", ""),
                disabled=bool(r.get("disabled")), focused=bool(r.get("focused")),
            )
            for i, r in enumerate(rows or [])
        ]

    async def find_element(
        self,
        *,
        role: Optional[str] = None,
        name: Optional[str] = None,
        text: Optional[str] = None,
        exact: bool = False,
        nth: int = 0,
    ):
        """Resolve a target to a Playwright element handle.

        Resolution order: explicit CSS → role+name → accessible text. Returns
        ``None`` when nothing matches, so the caller can fall back to pixels.
        """
        # 1. Role + accessible name, the browser-agent primary path.
        if role:
            selectors = ROLE_ALIASES.get(role.lower(), (role.lower(),))
            for sel in selectors:
                try:
                    locator = (
                        self._page.get_by_role(sel, name=name, exact=exact)
                        if name else self._page.get_by_role(sel, exact=exact)
                    )
                    if await locator.count() > nth:
                        return locator.nth(nth)
                except Exception:
                    continue

        # 2. Visible text.
        if text:
            try:
                loc = self._page.get_by_text(text, exact=exact)
                if await loc.count() > nth:
                    return loc.nth(nth)
            except Exception:
                pass

        # 3. Accessible-name substring across all roles.
        if name:
            try:
                loc = self._page.locator(
                    f'[aria-label*="{name}" i], [title*="{name}" i], '
                    f'[placeholder*="{name}" i], [value*="{name}" i]'
                )
                if await loc.count() > nth:
                    return loc.nth(nth)
            except Exception:
                pass

        return None

    # ── Pointer ───────────────────────────────────────────────────────────────

    async def mouse_move(self, x: int, y: int) -> ActionResult:
        self._cursor = (x, y)
        if self._cdp:
            try:
                await self._cdp.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": x, "y": y, "button": "none"},
                )
                return ActionResult(True, "mouse_move", f"({x},{y})", method="pixel")
            except Exception as e:
                logger.debug(f"cdp mouse_move failed: {e}")
        try:
            await self._page.mouse.move(x, y)
            return ActionResult(True, "mouse_move", f"({x},{y})", method="pixel")
        except Exception as e:
            return ActionResult(False, "mouse_move", error=str(e), method="pixel")

    async def _click_at(
        self, x: int, y: int, button: str, click_count: int, action: str
    ) -> ActionResult:
        """Pixel click with a real press/release pair.

        A single dispatched event is detected as synthetic; down+up with a
        small delay matches what a physical mouse produces.
        """
        target = {"x": x, "y": y, "button": button, "clickCount": click_count}
        if self._cdp:
            try:
                await self._cdp.send(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": x, "y": y, "button": "none"},
                )
                await self._cdp.send(
                    "Input.dispatchMouseEvent", {"type": "mousePressed", **target}
                )
                await asyncio.sleep(0.04)
                await self._cdp.send(
                    "Input.dispatchMouseEvent", {"type": "mouseReleased", **target}
                )
                self._cursor = (x, y)
                return ActionResult(
                    True, action, f"({x},{y}) {button}x{click_count}", method="pixel"
                )
            except Exception as e:
                logger.debug(f"cdp click failed, falling back: {e}")

        try:
            await self._page.mouse.move(x, y)
            await self._page.mouse.click(x, y, button=button, click_count=click_count)
            self._cursor = (x, y)
            return ActionResult(
                True, action, f"({x},{y}) {button}x{click_count}", method="pixel"
            )
        except Exception as e:
            return ActionResult(False, action, error=str(e), method="pixel")

    async def click(
        self,
        *,
        role: Optional[str] = None,
        name: Optional[str] = None,
        text: Optional[str] = None,
        selector: Optional[str] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        click_count: int = 1,
        nth: int = 0,
        timeout: Optional[int] = None,
    ) -> ActionResult:
        """Click an element, or a pixel coordinate if no element matches."""
        action = {
            (1, "left"): "left_click", (1, "right"): "right_click",
            (1, "middle"): "middle_click", (2, "left"): "double_click",
            (3, "left"): "triple_click",
        }.get((click_count, button), "left_click")

        # Pixel path.
        if x is not None and y is not None and not (role or name or text or selector):
            return await self._click_at(x, y, button, click_count, action)

        # Element path.
        handle = None
        try:
            if selector:
                loc = self._page.locator(selector)
                handle = loc.nth(nth) if loc.count() > nth else None
            else:
                handle = await self.find_element(
                    role=role, name=name, text=text, nth=nth
                )
        except Exception as e:
            logger.debug(f"element resolve failed: {e}")

        if handle is None:
            if x is not None and y is not None:
                return await self._click_at(x, y, button, click_count, action)
            return ActionResult(
                False, action,
                error="no matching element and no coordinates given",
                target=None,
            )

        info = ElementInfo(
            role=role or "element", name=name or text or selector or "", index=nth
        )
        try:
            await handle.scroll_into_view_if_needed(timeout=timeout or self._timeout)
            await handle.click(
                button=button, click_count=click_count,
                timeout=timeout or self._timeout,
            )
            return ActionResult(True, action, target=info, method="element")
        except Exception as e:
            # Element exists but is not clickable (covered, detached, animating).
            # Box centre is the honest fallback.
            try:
                box = await handle.bounding_box(timeout=2000)
                if box:
                    cx = int(box["x"] + box["width"] / 2)
                    cy = int(box["y"] + box["height"] / 2)
                    r = await self._click_at(cx, cy, button, click_count, action)
                    r.target = info
                    r.detail += " (via box centre)"
                    return r
            except Exception:
                pass
            return ActionResult(
                False, action, error=str(e), target=info, method="element"
            )

    async def hover(self, **kwargs: Any) -> ActionResult:
        handle = await self._resolve(kwargs)
        if handle is None:
            return ActionResult(False, "hover", error="no matching element")
        try:
            await handle.hover(timeout=self._timeout)
            return ActionResult(True, "hover", target=self._info(kwargs), method="pixel")
        except Exception as e:
            return ActionResult(False, "hover", error=str(e), method="pixel")

    async def drag(
        self, to: dict[str, Any], **kwargs: Any
    ) -> ActionResult:
        """Press at one point, move, release at another."""
        handle = await self._resolve(kwargs)
        try:
            if handle is not None:
                box = await handle.bounding_box()
                start = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                start = (to.get("from_x", 0), to.get("from_y", 0))
            end = (to.get("x", 0), to.get("y", 0))

            mouse = self._page.mouse
            await mouse.move(*start)
            await mouse.down()
            steps = 12
            for i in range(1, steps + 1):
                await mouse.move(
                    start[0] + (end[0] - start[0]) * i / steps,
                    start[1] + (end[1] - start[1]) * i / steps,
                )
                await asyncio.sleep(0.012)
            await mouse.up()
            return ActionResult(
                True, "drag", f"{start} → {end}", target=self._info(kwargs)
            )
        except Exception as e:
            return ActionResult(False, "drag", error=str(e))

    # ── Keyboard ──────────────────────────────────────────────────────────────

    async def type_text(
        self, text: str, *, role: Optional[str] = None,
        name: Optional[str] = None, selector: Optional[str] = None,
        delay: int = 25, nth: int = 0,
    ) -> ActionResult:
        """Focus a field and type. Types key-by-key so key handlers fire."""
        handle = await self._resolve(
            {"role": role, "name": name, "selector": selector, "nth": nth}
        )
        try:
            if handle is not None:
                await handle.click(timeout=self._timeout)
            else:
                await self._page.keyboard.press("Tab")
            await self._page.keyboard.type(text, delay=delay)
            return ActionResult(True, "type", f"{len(text)} chars",
                                target=self._info({"role": role, "name": name}))
        except Exception as e:
            return ActionResult(False, "type", error=str(e), method="keyboard")

    async def press_key(self, key: str, *, hold_ms: int = 0) -> ActionResult:
        """Press a key, optionally held. Accepts 'ctrl+a' / 'Control+KeyA'."""
        action = "hold_key" if hold_ms else "key"
        try:
            mapped = self._map_key(key)
            if hold_ms:
                parts = mapped.split("+")
                await self._page.keyboard.down(parts[-1])
                for m in reversed(parts[:-1]):
                    await self._page.keyboard.down(m)
                await asyncio.sleep(hold_ms / 1000)
                for m in parts[:-1]:
                    await self._page.keyboard.up(m)
                await self._page.keyboard.up(parts[-1])
            else:
                await self._page.keyboard.press(mapped)
            return ActionResult(True, action, mapped, method="keyboard")
        except Exception as e:
            return ActionResult(False, action, error=str(e), method="keyboard")

    async def scroll(
        self, delta_y: int = 0, delta_x: int = 0,
        *, selector: Optional[str] = None,
    ) -> ActionResult:
        """Scroll the page or a specific scroll container."""
        try:
            if selector:
                handle = await self._resolve({"selector": selector})
                if handle is not None:
                    await handle.hover()
            await self._page.mouse.wheel(delta_x, delta_y)
            return ActionResult(True, "scroll", f"dy={delta_y} dx={delta_x}")
        except Exception as e:
            return ActionResult(False, "scroll", error=str(e), method="pixel")

    # ── Observation ───────────────────────────────────────────────────────────

    async def screenshot(self, path: Optional[str] = None, full_page: bool = False):
        """Capture the viewport (or full page) as PNG bytes."""
        return await self._page.screenshot(path=path, full_page=full_page)

    async def cursor_position(self) -> ActionResult:
        return ActionResult(True, "cursor_position", str(self._cursor), method="pixel")

    async def wait(self, seconds: float = 1.0) -> ActionResult:
        await asyncio.sleep(seconds)
        return ActionResult(True, "wait", f"{seconds}s")

    async def wait_for_clearance(
        self, timeout: float = 30.0, interval: float = 1.0
    ) -> bool:
        """Wait for a Cloudflare ``cf_clearance`` cookie to be issued.

        The managed challenge resolves asynchronously — the page can sit at 200
        with challenge markup for several seconds before the cookie lands.
        Polling for the cookie is the only reliable success signal: the
        'challenge-platform' script tag is injected on served pages too, so
        string matching gives false positives.

        Returns True once the cookie is present, False on timeout.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                cookies = await self._page.context.cookies()
                if any(c.get("name") == "cf_clearance" for c in cookies):
                    return True
            except Exception:
                pass
            await asyncio.sleep(interval)
        return False

    async def clearance_cookies(self) -> dict[str, str]:
        """Return current cookies as a simple name→value mapping."""
        try:
            cookies = await self._page.context.cookies()
            return {c.get("name", ""): c.get("value", "") for c in cookies
                    if c.get("name")}
        except Exception as e:
            logger.debug(f"clearance_cookies failed: {e}")
            return {}

    async def wait_for_hydration(self, timeout: float = 15.0) -> bool:
        """Wait for a client-rendered app to actually mount.

        Returns True once a framework root holds real text, or immediately when
        the page has no framework root at all (a server-rendered page is
        hydrated by definition). Returns False on timeout so the caller can
        escalate to a heavier tier.
        """
        try:
            await self._page.wait_for_load_state(
                "networkidle", timeout=int(timeout * 1000)
            )
        except Exception:
            pass  # networkidle often never fires; fall through to root check

        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            try:
                state = await self._page.evaluate(
                    """() => {
                        const roots = ['#root','#app','#__next','#__nuxt',
                                       '[data-reactroot]'];
                        let sawRoot = false;
                        for (const r of roots) {
                            const el = document.querySelector(r);
                            if (!el) continue;
                            sawRoot = true;
                            if (el.innerText.trim().length > 200) {
                                return { mounted: true, sawRoot };
                            }
                        }
                        return { mounted: false, sawRoot };
                    }"""
                )
                if state.get("mounted"):
                    return True
                if not state.get("sawRoot"):
                    return True  # not an SPA; server-rendered is already ready
            except Exception:
                pass

            if asyncio.get_event_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.25)

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _resolve(self, kwargs: dict[str, Any]):
        try:
            return await self.find_element(
                role=kwargs.get("role"), name=kwargs.get("name"),
                text=kwargs.get("text"), selector=kwargs.get("selector"),
                nth=kwargs.get("nth", 0),
            )
        except Exception:
            return None

    def _info(self, kwargs: dict[str, Any]) -> Optional[ElementInfo]:
        role = kwargs.get("role")
        name = kwargs.get("name") or kwargs.get("text") or kwargs.get("selector")
        if not role and not name:
            return None
        return ElementInfo(
            role=role or "element", name=name or "", index=kwargs.get("nth", 0)
        )

    @staticmethod
    def _map_key(key: str) -> str:
        """Normalise a key or combo into Playwright's accepted form.

        Every part is alias-mapped, including modifiers: Playwright rejects
        ``ctrl``/``shift`` and expects ``Control``/``Shift``.
        """
        parts = [p.strip() for p in key.split("+") if p.strip()]
        if not parts:
            raise ValueError("empty key")
        out = [_KEY_ALIASES.get(p.lower(), p) for p in parts]
        # A lone modifier is a valid key press (Shift down/up).
        return "+".join(out)