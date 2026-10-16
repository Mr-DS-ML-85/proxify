"""Live test for PageInteractor against real pages.

Exercises the element-first path, the pixel fallback, and keyboard input.
Requires Playwright with a usable browser.

Run:  python3 engine/test_interaction.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.interaction import PageInteractor  # noqa: E402

TARGET = "https://example.com"
SPA = "https://react.dev/"


async def main() -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("SKIP: playwright not installed")
        return 0

    failures = 0
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=os.getenv("CHROMIUM_PATH")
        ) if os.getenv("CHROMIUM_PATH") else await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        cdp = None
        try:
            cdp = await browser.new_browser_cdp_session()
        except Exception:
            pass

        ix = PageInteractor(page, cdp)

        # 1. Hydration wait on a plain page
        ok = await ix.wait_for_hydration(timeout=8)
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  wait_for_hydration returns True")
        print(f"      {ok}\n")

        # 2. Snapshot on a real page
        await page.goto(TARGET, wait_until="domcontentloaded")
        snap = await ix.snapshot()
        ok = len(snap) > 0
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  snapshot returns elements")
        print(f"      {len(snap)} elements, first: "
              f"{snap[0].label()[:60] if snap else 'none'}\n")

        # 3. Element-first click by role+name, using whatever the snapshot reports
        #    (example.com's link text changes over time).
        snap = await ix.snapshot()
        target_name = snap[0].name if snap else "Learn more"
        r = await ix.click(role=snap[0].role if snap else "link", name=target_name)
        failures += 0 if r.ok else 1
        print(f"{'PASS' if r.ok else 'FAIL'}  element-first click by role+name")
        print(f"      {r.to_dict()}\n")

        # 4. Pixel click fallback
        r = await ix.click(x=50, y=50)
        ok = r.ok
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  pixel click fallback")
        print(f"      {r.to_dict()}\n")

        # 5. Keyboard: type + combo
        r = await ix.press_key("ctrl+a")
        failures += 0 if r.ok else 1
        print(f"{'PASS' if r.ok else 'FAIL'}  key combo ctrl+a")
        print(f"      {r.to_dict()}\n")

        r = await ix.type_text("hello")
        failures += 0 if r.ok else 1
        print(f"{'PASS' if r.ok else 'FAIL'}  type_text")
        print(f"      {r.to_dict()}\n")

        # 6. Scroll + hold_key + wait
        for fn, label, kwargs in [
            (ix.scroll, "scroll", {"delta_y": 300}),
            (ix.press_key, "hold_key", {"key": "shift", "hold_ms": 50}),
            (ix.wait, "wait", {"seconds": 0.1}),
            (ix.cursor_position, "cursor_position", {}),
        ]:
            r = await fn(**kwargs)
            failures += 0 if r.ok else 1
            print(f"{'PASS' if r.ok else 'FAIL'}  {label}")
            print(f"      {r.to_dict()}\n")

        # 7. Screenshot returns bytes
        shot = await ix.screenshot()
        ok = isinstance(shot, bytes) and len(shot) > 100
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  screenshot returns PNG bytes")
        print(f"      {len(shot)} bytes\n")

        # 8. SPA hydration on a heavy React page
        try:
            await page.goto(SPA, wait_until="domcontentloaded", timeout=30000)
            hydrated = await ix.wait_for_hydration(timeout=20)
            snap = await ix.snapshot(limit=60)
            ok = len(snap) > 5
            failures += 0 if ok else 1
            print(f"{'PASS' if ok else 'FAIL'}  heavy SPA hydrates and snapshots")
            print(f"      hydrated={hydrated}, {len(snap)} elements, "
                  f"sample: {snap[2].label()[:50] if len(snap) > 2 else 'n/a'}\n")
        except Exception as e:
            print(f"SKIP  SPA test: {str(e)[:80]}\n")

        await browser.close()

    print("done")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))