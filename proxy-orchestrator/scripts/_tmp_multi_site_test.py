import json, time, urllib.request, sys

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"

TARGETS = [
    ("REDDIT",      "https://www.reddit.com/r/technology/"),
    ("GITHUB",      "https://github.com/"),
    ("CLOUD-FURYL", "https://cloud.furylogic.com"),
    ("WWW-FURYL",   "https://www.furylogic.com"),
    ("GOOGLE",      "https://www.google.com/search?q=weather+in+London"),
    ("YANDEX",      "https://yandex.com/search/?text=weather+in+London"),
    ("ARXIV",       "https://arxiv.org/list/cs.AI/recent"),
]

def test(name, url):
    payload = {"url": url, "bypass_cache": True, "timeout": 55}
    req = urllib.request.Request(
        BASE + "/fetch",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=190) as r:
            d = json.loads(r.read().decode())
        html = d.get("html") or ""
        md = d.get("markdown") or ""
        low = html.lower()
        flags = []
        if "/sorry/" in low or "unusual traffic" in low: flags.append("GOOGLE_SORRY")
        if "please wait for verification" in low: flags.append("REDDIT_POW")
        if "<h3" in low: flags.append("HAS_H3")
        if "captcha" in low or "recaptcha" in low: flags.append("CAPTCHA")
        if "yvlrue" in low: flags.append("YVLRUE")
        print("%s [%.1fs] success=%s status=%s strat=%s ab=%s q=%s html=%dB md=%dB flags=%s err=%s" % (
            name, time.time() - t0, d.get("success"), d.get("status_code"),
            d.get("strategy_used"), d.get("antibot_score"), d.get("quality_score"),
            len(html), len(md), flags, (d.get("error") or "")[:70]))
    except Exception as e:
        print("%s FAIL: %s" % (name, repr(e)[:200]))

print("=== health: %s ===" % BASE)
try:
    with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
        print("health:", r.read().decode()[:120])
except Exception as e:
    print("health FAIL:", e)

for name, url in TARGETS:
    test(name, url)
print("=== DONE ===")
