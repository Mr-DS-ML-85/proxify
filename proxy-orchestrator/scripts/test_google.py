#!/usr/bin/env python3
"""Test Google search via Proxify — Smart 6-tier pipeline."""

import httpx

API_URL = "http://0.0.0.0:8080"
QUERY = "weather in London"
SEARCH_URL = f"https://www.google.com/search?q={QUERY.replace(' ', '+')}"


def test_via_api():
    """Test via REST API (shows full pipeline output)."""
    print("--- 🧪 Testing Google via REST API (Smart Pipeline) ---")
    try:
        resp = httpx.post(
            f"{API_URL}/fetch",
            json={
                "url": SEARCH_URL,
                "method": "GET",
                "bypass_cache": True,
            },
            timeout=60.0,
        )
        data = resp.json()
        print(f"Status: {data.get('status_code', '?')}")
        print(f"Strategy: {data.get('strategy_used', '?')}")
        print(f"Antibot Score: {data.get('antibot_score', '?')}")
        print(f"Quality Score: {data.get('quality_score', '?')}")
        print(f"HTML Length: {len(data.get('html', ''))}")
        print(f"Latency: {data.get('latency', 0):.2f}s")
        print(f"Retries: {data.get('retries', 0)}")

        meta = data.get("metadata", {})
        print(f"Antibot Status: {meta.get('antibot_status', '?')}")
        print(f"Quality Usable: {meta.get('quality_usable', '?')}")
        print(f"Antibot Reasons: {meta.get('antibot_reasons', [])}")

        html = data.get("html", "")
        has_results = "<h3" in html.lower()
        print(f"Has <h3> results: {has_results}")

        if data.get("success") and has_results:
            print("✅ SUCCESS: Search results found!")
        elif data.get("success"):
            print("⚠️  SUCCESS but no <h3> — may be JS shell or limited results")
        else:
            print(f"❌ FAILURE: {data.get('error', 'Unknown')}")

    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    test_via_api()
