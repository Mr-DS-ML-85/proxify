"""Regression test for the gated anti-bot rules.

Cases are the real responses observed against live targets:
  - nowsecure.nl served a Cloudflare pass page (must NOT be flagged)
  - Reddit served 100KB of hydrated content at 0.38 script ratio (must NOT be flagged)
  - A real Cloudflare 403 challenge (MUST still be flagged)

Run:  python3 engine/test_antibot_regression.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.antibot import detect_antibot  # noqa: E402


def _page(body: str, scripts: str = "") -> str:
    return (
        "<!DOCTYPE html><html><head><title>t</title>"
        + scripts +
        "</head><body>" + body + "</body></html>"
    )


# A healthy page whose <script> merely *mentions* a vendor. This is the exact
# false positive that made nowsecure.nl and Reddit score as suspicious.
CLEAN_WITH_VENDOR_MENTION = _page(
    "<h1>Reddit</h1><p>The heart of the internet</p>"
    "<div class='feed'>" + "<p>Real post content here.</p>" * 40 + "</div>",
    scripts='<script>var cfg={"captcha":"cloudflare","blocked":false};</script>',
)

# A real Cloudflare managed-challenge 403. Must still be caught.
REAL_BLOCK = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
    "<h1>Checking if the site connection is secure</h1>"
    "<div>Verify you are human</div>"
    "<p>Please complete the security check</p>"
    "</body></html>"
)

# Thin SPA shell: high script ratio AND almost no visible text. Should flag.
JS_SHELL = _page(
    "<div id='root'></div><noscript>Please enable JavaScript</noscript>",
    scripts="<script>" + "var x=1;" * 900 + "</script>",
)


def run() -> int:
    cases = [
        (
            "clean page mentioning a vendor in <script>",
            CLEAN_WITH_VENDOR_MENTION, 200, "https://www.reddit.com/r/programming/",
            "ok", "vendor mention must not penalise",
        ),
        (
            "real Cloudflare 403 challenge",
            REAL_BLOCK, 403, "https://example.com/",
            "blocked", "genuine block must still be caught",
        ),
        (
            "thin JS shell, no visible content",
            JS_SHELL, 200, "https://spa.example.com/",
            "suspicious", "empty hydration shell must flag",
        ),
    ]

    failures = 0
    for name, html, status, url, expected, note in cases:
        r = detect_antibot(html, status_code=status, final_url=url)
        ok = r.status == expected
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        print(f"      status={r.status} (expected {expected}) score={r.score} "
              f"reasons={r.reasons}")
        print(f"      -> {note}\n")

    print(f"{len(cases) - failures}/{len(cases)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())