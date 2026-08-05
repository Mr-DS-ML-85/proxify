"""
FlareSolverr Strategy — Cloudflare & DDoS-Guard bypass using Selenium + undetected-chromedriver.
Connects to FlareSolverr's REST API for JS challenge solving.
"""

import logging
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from strategies.base import BaseStrategy, FetchRequest, FetchResult
from services.cookie_jar import CookieManager
from config import config

logger = logging.getLogger(__name__)


class FlareSolverrStrategy(BaseStrategy):
    """
    FlareSolverr integration for Cloudflare challenge bypass.

    FlareSolverr API (from docs):
    - POST /v1 with cmd="request.get" to fetch a URL
    - sessions.create/sessions.destroy for persistent sessions
    - Returns: solution.response (HTML), solution.cookies, solution.userAgent
    - session_ttl_minutes for auto-rotation
    - Proxy pass-through: {"proxy": {"url": "http://..."}}
    """

    def __init__(self, cookie_manager: CookieManager) -> None:
        self._cookie_manager = cookie_manager
        self._base_url = config.FLARESOLVERR_URL
        self._enabled = config.FLARESOLVERR_ENABLED
        self._max_timeout = config.FLARESOLVERR_MAX_TIMEOUT
        self._active_sessions: dict[str, str] = {}  # domain -> session_id
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def name(self) -> str:
        return "flaresolverr"

    @property
    def priority(self) -> int:
        return 30  # Third in escalation order

    async def initialize(self) -> None:
        """Initialize HTTP client and test FlareSolverr connectivity."""
        if not self._enabled:
            logger.info("FlareSolverr strategy disabled via config")
            return

        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
        try:
            # Quick health check
            resp = await self._client.post(
                self._base_url,
                json={"cmd": "sessions.list"},
                timeout=10.0,
            )
            if resp.status_code == 200:
                logger.info(f"FlareSolverr connected at {self._base_url}")
            else:
                logger.warning(
                    f"FlareSolverr returned status {resp.status_code}, may not be ready"
                )
        except Exception as e:
            logger.warning(f"FlareSolverr not reachable at {self._base_url}: {e}")
            self._enabled = False

    async def _create_session(self, domain: str) -> Optional[str]:
        """Create a persistent FlareSolverr session for a domain."""
        if not self._client:
            return None
        try:
            payload = {"cmd": "sessions.create"}
            resp = await self._client.post(self._base_url, json=payload)
            data = resp.json()
            session_id = data.get("session")
            if session_id:
                self._active_sessions[domain] = session_id
                logger.debug(f"FlareSolverr session created for {domain}: {session_id}")
            return session_id
        except Exception as e:
            logger.debug(f"Failed to create FlareSolverr session: {e}")
            return None

    async def _destroy_session(self, session_id: str) -> None:
        """Destroy a FlareSolverr session."""
        if not self._client:
            return
        try:
            await self._client.post(
                self._base_url,
                json={"cmd": "sessions.destroy", "session": session_id},
            )
        except Exception:
            pass

    async def fetch(self, request: FetchRequest) -> FetchResult:
        """Fetch URL through FlareSolverr's challenge-solving proxy."""
        start_time = time.monotonic()

        if not self._enabled or not self._client:
            return self._make_result(
                request, start_time,
                success=False,
                error="FlareSolverr not available",
            )

        domain = urlparse(request.url).netloc

        try:
            # Build FlareSolverr request payload
            # FlareSolverr natively supports request.get and request.post.
            # For other methods we still use request.post with a method
            # override so the underlying browser engine can handle it.
            method = request.method.upper() if request.method else "GET"
            if method == "POST":
                cmd = "request.post"
            elif method == "GET":
                cmd = "request.get"
            else:
                cmd = "request.post"
            payload: dict = {
                "cmd": cmd,
                "url": request.url,
                "maxTimeout": self._max_timeout,
            }

            # Use persistent session if available
            session_id = self._active_sessions.get(domain)
            if not session_id:
                session_id = await self._create_session(domain)
            if session_id:
                payload["session"] = session_id
                payload["session_ttl_minutes"] = 30

            # Add proxy if specified
            if request.proxy_url:
                payload["proxy"] = {"url": request.proxy_url}

            # Add body for POST and other methods that support it
            if method in ("POST", "PUT", "PATCH") and request.body:
                payload["postData"] = request.body.decode("utf-8", errors="replace")

            # Make the request to FlareSolverr
            resp = await self._client.post(
                self._base_url,
                json=payload,
                timeout=self._max_timeout / 1000 + 10,  # Convert ms to s + buffer
            )

            data = resp.json()

            if data.get("status") != "ok":
                error_msg = data.get("message", "Unknown FlareSolverr error")
                logger.debug(f"FlareSolverr error for {request.url}: {error_msg}")
                return self._make_result(
                    request, start_time,
                    success=False,
                    error=error_msg,
                )

            solution = data.get("solution", {})
            status_code = solution.get("status", 0)
            html = solution.get("response", "")
            final_url = solution.get("url", request.url)
            cookies_list = solution.get("cookies", [])
            user_agent = solution.get("userAgent", "")
            response_headers = solution.get("headers", {})

            # Store cookies from FlareSolverr response
            if cookies_list:
                await self._cookie_manager.import_from_list(domain, cookies_list)
                cookie_dict = {c["name"]: c["value"] for c in cookies_list if "name" in c}
            else:
                cookie_dict = {}

            result = self._make_result(
                request, start_time,
                success=200 <= status_code < 400,
                status_code=status_code,
                final_url=final_url,
                headers=response_headers,
                html=html,
                cookies=cookie_dict,
            )

            # Set metadata about FlareSolverr-specific info
            result.metadata["flaresolverr_user_agent"] = user_agent
            result.metadata["flaresolverr_session"] = session_id

            if result.is_blocked:
                result.success = False

            return result

        except httpx.TimeoutException:
            logger.warning(f"FlareSolverr timeout for {request.url}")
            return self._make_result(
                request, start_time,
                success=False,
                error="FlareSolverr timeout",
            )
        except Exception as e:
            logger.warning(f"FlareSolverr error for {request.url}: {e}")
            return self._make_result(
                request, start_time,
                success=False,
                error=str(e),
            )

    async def shutdown(self) -> None:
        """Destroy all active sessions and close client."""
        for domain, sid in list(self._active_sessions.items()):
            await self._destroy_session(sid)
        self._active_sessions.clear()
        if self._client:
            await self._client.aclose()
