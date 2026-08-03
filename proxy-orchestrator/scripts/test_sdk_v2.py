import json
import logging
import sys
from proxy_orchestrator import OrchestratorClient

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("sdk_test")

def test_v2():
    client = OrchestratorClient()
    
    print("\n--- 🧪 Proxify SDK Verification (v1.0.0) ---")
    
    # 1. Basic Fetch
    print("\n[1/3] Testing Basic Fetch (Simple Strategy)...")
    try:
        res = client.fetch("https://httpbin.org/get")
        if res.get("success"):
            print(f"✅ Success! Strategy: {res.get('strategy_used')} | Latency: {res.get('latency')}s")
        else:
            print(f"❌ Failed: {res.get('error')}")
    except Exception as e:
        print(f"❌ Error: {e}")

    # 2. Strategy Forcing (FlareSolverr/Scrapling)
    print("\n[2/3] Testing Strategy Forcing (Scrapling)...")
    try:
        # Using camelCase alias 'forceStrategy' as per SDK Guide
        res = client.fetch("https://httpbin.org/user-agent", forceStrategy="scrapling")
        if res.get("success"):
            print(f"✅ Success! Strategy: {res.get('strategy_used')}")
            # print(f"User-Agent: {json.loads(res.get('html')).get('user-agent')}")
        else:
            print(f"❌ Failed: {res.get('error')}")
    except Exception as e:
        print(f"❌ Error: {e}")

    # 3. Cache Bypass
    print("\n[3/3] Testing Cache Bypass...")
    try:
        res = client.fetch("https://httpbin.org/ip", bypassCache=True)
        if res.get("success"):
            print(f"✅ Success! Cached: {res.get('cached')}")
        else:
            print(f"❌ Failed: {res.get('error')}")
    except Exception as e:
        print(f"❌ Error: {e}")

    print("\n--- ✨ Verification Complete ---")

if __name__ == "__main__":
    test_v2()
