import sys
import os
import asyncio
import logging

# Ensure we're using the local SDK
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "clients/python")))

from proxy_orchestrator import OrchestratorClient, BypassFailedError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sdk_test")

async def test_python_sdk():
    print("🚀 Initializing OrchestratorClient...")
    client = OrchestratorClient()
    print("🚀 Client initialized.")
    
    logger.info("🧪 Testing Health Check...")
    print("🚀 Calling get_health()...")
    health = client.get_health()
    print(f"🚀 get_health() returned: {health}")
    logger.info(f"Health: {health}")
    assert health["status"] == "ok"
    
    logger.info("🧪 Testing Simple Fetch (REST)...")
    result = client.fetch("https://checkip.amazonaws.com")
    logger.info(f"Fetched IP: {result['html'].strip()} via {result['strategy_used']}")
    assert result["success"] is True
    
    logger.info("🧪 Testing Session Persistence (REST)...")
    # First request
    r1 = client.fetch("https://httpbin.org/get", session_id="test-session-1", bypass_cache=True)
    import json
    data1 = json.loads(r1["html"])
    ua1 = data1["headers"].get("User-Agent")
    
    # Second request with same session_id
    r2 = client.fetch("https://httpbin.org/get", session_id="test-session-1", bypass_cache=True)
    data2 = json.loads(r2["html"])
    ua2 = data2["headers"].get("User-Agent")
    
    logger.info(f"Session 1 UA: {ua1}")
    logger.info(f"Session 2 UA: {ua2}")
    assert ua1 == ua2, "Session UA mismatch!"
    
    # Third request with new session_id
    r3 = client.fetch("https://httpbin.org/get", session_id="test-session-2", bypass_cache=True)
    data3 = json.loads(r3["html"])
    ua3 = data3["headers"].get("User-Agent")
    logger.info(f"Session 3 UA: {ua3}")
    assert ua1 != ua3, "Sessions should have different UAs!"
    
    logger.info("🧪 Testing Proxy Client Integration...")
    with client.get_proxy_client() as proxy:
        resp = proxy.get("https://google.com")
        logger.info(f"Proxy Client Success: {resp.status_code}")
        assert resp.status_code == 200
        
    logger.info("✅ All Python SDK Tests Passed!")

if __name__ == "__main__":
    try:
        asyncio.run(test_python_sdk())
    except Exception as e:
        logger.error(f"❌ SDK Test Failed: {e}")
        sys.exit(1)
