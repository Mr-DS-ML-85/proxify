"""
Python Client Library — Easy integration for Python scripts.
Supports both proxy mode and API mode.
"""

import json
from typing import Any, Optional
from dataclasses import dataclass


@dataclass
class FetchResponse:
    """Response from the orchestrator."""
    success: bool
    status_code: int
    url: str
    final_url: str
    html: str
    headers: dict[str, str]
    strategy_used: str
    latency: float
    cached: bool
    error: Optional[str] = None


class ProxyOrchestratorClient:
    """
    Python client for the Proxify.

    Usage (API mode):
        client = ProxyOrchestratorClient(api_url="http://localhost:8080")
        result = await client.fetch("https://example.com")
        print(result.html)

    Usage (Proxy mode):
        import httpx
        client = httpx.Client(proxy="http://localhost:8888")
        response = client.get("https://example.com")
    """

    def __init__(
        self,
        api_url: str = "http://localhost:8080",
        proxy_url: str = "http://localhost:8888",
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._proxy_url = proxy_url

    async def fetch(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[dict] = None,
        body: Optional[str] = None,
        force_strategy: Optional[str] = None,
        bypass_cache: bool = False,
        timeout: float = 30.0,
    ) -> FetchResponse:
        """
        Fetch a URL through the orchestrator's REST API.

        Args:
            url: URL to fetch
            method: HTTP method (GET, POST, etc.)
            headers: Extra headers to send
            body: Request body for POST
            force_strategy: Force a specific strategy (simple, scrapling, flaresolverr)
            bypass_cache: Skip cache lookup
            timeout: Request timeout in seconds
        """
        import httpx

        payload = {
            "url": url,
            "method": method,
            "headers": headers or {},
            "timeout": timeout,
            "bypass_cache": bypass_cache,
        }
        if body:
            payload["body"] = body
        if force_strategy:
            payload["force_strategy"] = force_strategy

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._api_url}/fetch",
                json=payload,
                timeout=timeout + 10,
            )
            data = resp.json()

        return FetchResponse(
            success=data.get("success", False),
            status_code=data.get("status_code", 0),
            url=data.get("url", url),
            final_url=data.get("final_url", url),
            html=data.get("html", ""),
            headers=data.get("headers", {}),
            strategy_used=data.get("strategy_used", ""),
            latency=data.get("latency", 0.0),
            cached=data.get("cached", False),
            error=data.get("error"),
        )

    async def health(self) -> dict:
        """Check orchestrator health."""
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self._api_url}/health", timeout=5.0)
            return resp.json()

    async def stats(self, domain: Optional[str] = None) -> dict:
        """Get domain statistics."""
        import httpx
        params = {"domain": domain} if domain else {}
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._api_url}/stats",
                params=params,
                timeout=5.0,
            )
            return resp.json()

    def get_proxy_dict(self) -> dict[str, str]:
        """Get proxy configuration dict for use with requests/httpx."""
        return {
            "http://": self._proxy_url,
            "https://": self._proxy_url,
        }

    def get_proxy_url(self) -> str:
        """Get the proxy URL string."""
        return self._proxy_url
