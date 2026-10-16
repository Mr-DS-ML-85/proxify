"""Tests for the universal heavy-JS SPA cleaner.

Cases mirror the real failure modes observed against live SPAs:
  - A hydrated React app whose content sits inside <main>
  - A Reddit-style feed with heavy nav/footer furniture
  - An unhydrated shell: big <script>, empty #root, "Please enable JavaScript"

Run:  python3 engine/test_spa_cleaner.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.dom_cleaner import clean_spa  # noqa: E402

HYDRATED_REACT = """<!DOCTYPE html><html><head><title>App</title></head><body>
<nav><a href="/a">Home</a><a href="/b">Docs</a></nav>
<div id="root">
  <main><h1>Quarterly Report</h1>
  <p>""" + ("Revenue grew substantially across every region this quarter. " * 12) + """</p>
  <p>""" + ("Operating margin expanded as infrastructure costs declined. " * 12) + """</p>
  </main>
</div>
<footer><p>Copyright 2026</p></footer>
<script>window.__DATA__={"a":1};</script>
</body></html>"""

REDDIT_LIKE = """<!DOCTYPE html><html><body>
<nav>Skip to main content Best Hot New Top</nav>
<main>""" + ("<p>Some genuinely interesting discussion about compilers.</p>" * 20) + """</main>
<aside>Related communities sidebar widget</aside>
<footer>Reddit terms help privacy</footer>
</body></html>"""

UNHYDRATED_SHELL = """<!DOCTYPE html><html><head><title>SPA</title></head><body>
<noscript>You need to enable JavaScript to run this app.</noscript>
<div id="root"></div>
<script>""" + ("var a=1;" * 1200) + """</script>
</body></html>"""


def run() -> int:
    failures = 0

    # 1. Hydrated React app
    r = clean_spa(HYDRATED_REACT, url="https://spa.example.com/")
    ok = (
        r.success
        and "Quarterly Report" in r.text_content
        and "Revenue grew" in r.text_content
        and "window.__DATA__" not in r.text_content
    )
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  hydrated SPA keeps content, drops script")
    print(f"      method={r.method} words={r.word_count}")
    print(f"      text: {r.text_content[:90]!r}\n")

    # 2. Boilerplate removal
    r = clean_spa(REDDIT_LIKE, url="https://www.reddit.com/r/x/")
    kept_discussion = "interesting discussion" in r.text_content
    dropped_nav = "Skip to main content" not in r.text_content
    ok = r.success and kept_discussion and dropped_nav
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  boilerplate nav/footer removed, content kept")
    print(f"      method={r.method} words={r.word_count}")
    print(f"      nav_gone={dropped_nav} content_kept={kept_discussion}\n")

    # 3. Unhydrated shell must be reported, not silently "succeeded"
    r = clean_spa(UNHYDRATED_SHELL, url="https://empty.example.com/")
    ok = (not r.success) and r.error == "unhydrated_spa"
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  unhydrated shell flagged, not faked")
    print(f"      success={r.success} error={r.error} words={r.word_count}\n")

    print(f"{3 - failures}/3 passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())