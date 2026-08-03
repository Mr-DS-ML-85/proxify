import asyncio
import json
import sys
import time
import urllib.request
import concurrent.futures

BASE = "http://localhost:8080"

URLS = [
    ("reddit", "https://old.reddit.com/r/technology/"),
    ("github", "https://github.com/trending"),
    ("google", "https://www.google.com/search?q=synthetic+intelligence"),
    ("yandex", "https://yandex.com/search/?text=synthetic+intelligence"),
]

print_lock = asyncio.Lock()


def sync_fetch(body, timeout):
    req = urllib.request.Request(
        BASE + "/fetch",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


async def one(pool, name, url, i, timeout):
    body = {"url": url, "bypass_cache": True, "timeout": timeout}
    t0 = time.time()
    loop = asyncio.get_running_loop()
    try:
        r = await loop.run_in_executor(pool, sync_fetch, body, timeout + 10)
        elapsed = round(time.time() - t0, 2)
        m = r.get("markdown_metadata", {})
        out = {
            "domain": name, "i": i, "success": r.get("success"),
            "status": r.get("status_code"), "latency": r.get("latency"),
            "elapsed": elapsed, "words": m.get("word_count", 0),
            "strategy": r.get("strategy_used"), "method": m.get("extraction_method", ""),
            "error": (r.get("error") or "")[:50],
            "final_url": (r.get("final_url") or "")[:70],
        }
        async with print_lock:
            print(f"  [{name:7s}] #{i} {'OK ' if out['success'] else 'FAIL'} "
                  f"st={out['status']} strat={out['strategy']} words={out['words']} "
                  f"{out['elapsed']}s err={(out['error'] or '')[:35]}", flush=True)
        return out
    except Exception as e:
        async with print_lock:
            print(f"  [{name:7s}] #{i} EXC {str(e)[:60]}", flush=True)
        return {"domain": name, "i": i, "success": False, "status": 0,
                "elapsed": round(time.time() - t0, 2), "error": str(e)[:50]}


async def main():
    per_domain = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    concurrency = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 15
    deadline = time.time() + 200

    sem = asyncio.Semaphore(concurrency)
    results = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency + 2)

    async def gate(name, url, i):
        async with sem:
            if time.time() > deadline:
                return None
            return await one(pool, name, url, i, timeout)

    tasks = [gate(n, u, i) for n, u in URLS for i in range(per_domain)]
    t_start = time.time()
    for fut in asyncio.as_completed(tasks):
        r = await fut
        if r:
            results.append(r)

    wall = round(time.time() - t_start, 1)
    by = {}
    for r in results:
        by.setdefault(r["domain"], []).append(r)

    print("\n===================== SUMMARY =====================")
    for domain, rows in by.items():
        ok = [r for r in rows if r["success"]]
        lat = [r["elapsed"] for r in rows]
        words = [r["words"] for r in ok]
        blocks = [r for r in rows
                  if "sorry" in (r.get("final_url") or "").lower()
                  or "captcha" in (r.get("final_url") or "").lower()
                  or "challenge" in (r.get("final_url") or "").lower()
                  or "checkrobot" in (r.get("final_url") or "").lower()]
        strat = {}
        for r in ok:
            strat[r["strategy"]] = strat.get(r["strategy"], 0) + 1
        print(f"\n{domain.upper()}  —  {len(rows)} requests")
        if ok:
            print(f"  OK: {len(ok)}/{len(rows)}   WORDS: min={min(words)} med={sorted(words)[len(words)//2]} max={max(words)}")
            print(f"  LATENCY: min={min(lat):.2f}s med={sorted(lat)[len(lat)//2]:.2f}s max={max(lat):.2f}s")
            print(f"  STRATEGIES: {strat}")
        if blocks:
            print(f"  BLOCK/CAPTCHA: {len(blocks)}")
            for b in blocks[:3]:
                print(f"    - {b['final_url']}")
        for f in [r for r in rows if not r["success"]][:3]:
            print(f"  FAIL: st={f['status']} err={f['error']} lat={f['elapsed']}s")

    total_ok = sum(1 for r in results if r["success"])
    print(f"\nTOTAL: {len(results)} requests, {total_ok} OK, wall={wall}s")
    pool.shutdown()


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print("\ninterrupted")
