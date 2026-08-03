"""
Cross-Strategy Session Cookie & TLS Profile Sharing

Solves:
  - ❌ Cookies are not shared between curl_cffi → Scrapling → Playwright
  - ❌ Each strategy starts with a fresh session, triggering more anti-bot challenges
  - ❌ TLS profiles are not persistent across strategies for the same domain

Architecture:
  - Central cookie jar shared by ALL strategies (Lib++ + existing orchestrator)
  - Bridges to existing proxy-orchestrator CookieManager via adapter
  - TLS profile mapped to cookies (same TLS profile = same cookie session)
  - Automatic cookie import/export between lightweight and browser strategies
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .types import SessionCookie

logger = logging.getLogger(__name__)

# Reference to external CookieManager (set by adapter)
_external_cookie_manager: Optional[Any] = None


def bridge_to_external_cookie_manager(cookie_manager: Any) -> None:
    """
    Bridge Lib++ cookie sharing to an external CookieManager instance.

    Call this during initialization to connect Lib++'s cross-strategy cookie
    sharing with the existing proxy-orchestrator CookieManager.
    This ensures cookies flow both ways: existing strategies → Lib++ and Lib++ → existing strategies.
    """
    global _external_cookie_manager
    _external_cookie_manager = cookie_manager
    logger.info("CrossStrategyCookieJar bridged to external CookieManager")


class CrossStrategyCookieJar:
    """
    Central cookie jar shared across ALL strategies.

    Features:
    - Per-domain cookie storage with TTL
    - TLS profile tracking per cookie
    - Strategy source tracking
    - Bidirectional sync with external CookieManager (proxy-orchestrator)
    - Cookie import/export between lightweight and browser strategies
    """

    def __init__(self, default_ttl: float = 3600.0):
        self._cookies: dict[str, dict[str, SessionCookie]] = {}
        self._domain_tls_map: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._default_ttl = default_ttl

    async def set_cookie(
        self, domain: str, name: str, value: str,
        path: str = "/", secure: bool = True, http_only: bool = True,
        source_strategy: str = "", tls_profile: str = "",
        ttl: Optional[float] = None,
    ) -> None:
        async with self._lock:
            if domain not in self._cookies:
                self._cookies[domain] = {}
            self._cookies[domain][name] = SessionCookie(
                domain=domain, name=name, value=value, path=path,
                secure=secure, http_only=http_only,
                expires=time.monotonic() + (ttl or self._default_ttl),
                source_strategy=source_strategy, tls_profile_used=tls_profile,
            )
            if tls_profile:
                self._domain_tls_map[domain] = tls_profile

        # Sync to external CookieManager if bridged
        if _external_cookie_manager:
            try:
                await _external_cookie_manager.set_cookies(
                    domain, {name: value}, ttl=ttl
                )
            except Exception as e:
                logger.debug(f"External cookie bridge set failed for {domain}: {e}")

    async def set_cookies_batch(
        self, domain: str, cookies: dict[str, str],
        source_strategy: str = "", tls_profile: str = "",
    ) -> None:
        for name, value in cookies.items():
            await self.set_cookie(
                domain=domain, name=name, value=value,
                source_strategy=source_strategy, tls_profile=tls_profile,
            )

    async def get_cookies(self, domain: str) -> dict[str, str]:
        async with self._lock:
            domain_cookies = self._cookies.get(domain, {})
            now = time.monotonic()
            result = {}
            expired = []
            for name, cookie in domain_cookies.items():
                if now < cookie.expires:
                    result[name] = cookie.value
                else:
                    expired.append(name)
            for name in expired:
                del domain_cookies[name]

        # Lazy pull from the external CookieManager when the local jar is empty
        # for this domain — this is what makes the imported Netscape cookie file
        # (scripts/brave_cookies.py) actually reach curl_cffi_plus/tls_rotator
        # and every other Lib++ HTTP strategy. Without this, the real session
        # cookies silently never left the central jar and Google/Reddit saw a
        # cookie-less client (which they flag).
        if not result and _external_cookie_manager:
            try:
                external = await _external_cookie_manager.get_cookies(domain)
                if external:
                    await self.set_cookies_batch(
                        domain=domain, cookies=external,
                        source_strategy="external_orchestrator",
                    )
                    result.update(external)
            except Exception as e:
                logger.debug(f"External cookie lazy-pull failed for {domain}: {e}")
        return result

    async def get_cookie_header(self, domain: str) -> str:
        cookies = await self.get_cookies(domain)
        if not cookies:
            return ""
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    async def get_best_tls_profile(self, domain: str) -> str:
        async with self._lock:
            return self._domain_tls_map.get(domain, "")

    async def import_from_external(self, domain: str) -> int:
        """
        Import cookies from the external CookieManager (proxy-orchestrator).
        Call this to sync cookies from existing strategies into Lib++.
        """
        if not _external_cookie_manager:
            return 0
        try:
            external_cookies = await _external_cookie_manager.get_cookies(domain)
            if external_cookies:
                await self.set_cookies_batch(
                    domain=domain, cookies=external_cookies,
                    source_strategy="external_orchestrator",
                )
                return len(external_cookies)
        except Exception as e:
            logger.debug(f"External cookie bridge get failed for {domain}: {e}")
        return 0

    async def export_to_browser(self, domain: str) -> list[dict]:
        cookies = await self.get_cookies(domain)
        return [
            {"name": k, "value": v, "domain": domain, "path": "/",
             "secure": True, "httpOnly": True}
            for k, v in cookies.items()
        ]

    async def clear_domain(self, domain: str) -> None:
        async with self._lock:
            self._cookies.pop(domain, None)
            self._domain_tls_map.pop(domain, None)

    async def cleanup(self) -> int:
        async with self._lock:
            now = time.monotonic()
            removed = 0
            for domain, cookies in list(self._cookies.items()):
                expired = [n for n, c in cookies.items() if now > c.expires]
                for name in expired:
                    del cookies[name]
                    removed += 1
                if not cookies:
                    del self._cookies[domain]
            return removed

    @property
    def stats(self) -> dict:
        return {
            "domains": len(self._cookies),
            "total_cookies": sum(len(c) for c in self._cookies.values()),
            "tracked_tls_profiles": len(self._domain_tls_map),
            "bridged_to_external": _external_cookie_manager is not None,
        }


class SessionCookieSharing:
    """Orchestrates cross-strategy cookie sharing across ALL strategies."""

    def __init__(self, cookie_jar: CrossStrategyCookieJar):
        self._cookie_jar = cookie_jar

    async def share_across_strategies(
        self, domain: str, cookies: dict[str, str],
        source_strategy: str, tls_profile: str,
    ) -> None:
        logger.info(
            f"Sharing {len(cookies)} cookies from '{source_strategy}' "
            f"(TLS: {tls_profile}) across ALL strategies for {domain}"
        )
        await self._cookie_jar.set_cookies_batch(
            domain=domain, cookies=cookies,
            source_strategy=source_strategy, tls_profile=tls_profile,
        )

    async def get_shared_cookies(self, domain: str, for_strategy: str) -> dict[str, str]:
        # First sync from external CookieManager
        await self._cookie_jar.import_from_external(domain)
        cookies = await self._cookie_jar.get_cookies(domain)
        best_tls = await self._cookie_jar.get_best_tls_profile(domain)
        if cookies:
            logger.debug(
                f"Providing {len(cookies)} shared cookies to '{for_strategy}' "
                f"(best TLS: {best_tls}) for {domain}"
            )
        return cookies

    async def get_best_tls_for_domain(self, domain: str) -> str:
        return await self._cookie_jar.get_best_tls_profile(domain)


cross_strategy_jar = CrossStrategyCookieJar()
session_cookie_sharing = SessionCookieSharing(cross_strategy_jar)
