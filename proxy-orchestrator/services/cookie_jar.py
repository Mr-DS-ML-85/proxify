"""
Cookie Jar — Per-domain cookie persistence with sticky cookies and TTL.
Supports import/export between strategies, and bulk import of real
user session cookies (Netscape format) for stealth.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from http.cookies import SimpleCookie
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class DomainCookies:
    """Cookies for a specific domain."""
    cookies: dict[str, str] = field(default_factory=dict)
    last_updated: float = 0.0
    ttl: float = 3600.0  # 1 hour default


class CookieManager:
    """
    Per-domain cookie persistence with sticky assignment and TTL cleanup.
    Cookies can be imported/exported between strategies.
    """

    def __init__(self, default_ttl: float = 3600.0) -> None:
        self._default_ttl = default_ttl
        self._jars: dict[str, DomainCookies] = {}
        self._lock = asyncio.Lock()
        self._source_file: Optional[str] = None
        self._last_loaded: float = 0.0

    @staticmethod
    def _normalize(domain: str) -> str:
        """Strip leading dots and lowercase (google.com == .google.com)."""
        return domain.lstrip(".").lower()

    def _matching_keys(self, domain: str) -> list[str]:
        """Jar keys that apply to a request domain (exact + parent domains)."""
        d = self._normalize(domain)
        parts = d.split(".")
        keys = [d]
        for i in range(1, len(parts) - 1):
            keys.append(".".join(parts[i:]))
        return keys

    async def get_cookies(self, domain: str) -> dict[str, str]:
        """Get all cookies that apply to a domain (exact + parent suffixes)."""
        async with self._lock:
            now = time.monotonic()
            merged: dict[str, str] = {}
            for key in self._matching_keys(domain):
                entry = self._jars.get(key)
                if entry is None:
                    continue
                if now - entry.last_updated > entry.ttl:
                    del self._jars[key]
                    continue
                merged.update(entry.cookies)
            return merged

    async def set_cookies(
        self,
        domain: str,
        cookies: dict[str, str],
        ttl: Optional[float] = None,
    ) -> None:
        """Set cookies for a domain. Merges with existing cookies."""
        async with self._lock:
            key = self._normalize(domain)
            if key not in self._jars:
                self._jars[key] = DomainCookies(
                    ttl=ttl or self._default_ttl,
                )
            entry = self._jars[key]
            entry.cookies.update(cookies)
            entry.last_updated = time.monotonic()

    # ── Real user session cookies (Netscape format) ──────────────────────

    async def import_netscape_file(self, path: str, ttl: Optional[float] = None) -> int:
        """
        Load a Netscape-format cookie file (e.g. exported from the user's real
        browser by scripts/brave_cookies.py) into the jar. Returns count.

        These cookies are then automatically sent by the HTTP strategies and
        used to seed the GUI Chrome — the strongest trust signal for
        Google/Reddit-class anti-bots.
        """
        if not path or not os.path.exists(path):
            return 0
        count = 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _flag, _path, _secure, _expires, name, value = parts[:7]
                if not name or not value:
                    continue
                await self.set_cookies(
                    self._normalize(domain), {name: value},
                    ttl=ttl or self._default_ttl * 24,  # 24h default
                )
                count += 1
        if count:
            self._source_file = path
            self._last_loaded = time.monotonic()
            logger.info(
                f"CookieManager: imported {count} real session cookies from {path}"
            )
        return count

    def status(self) -> dict:
        """Summary for /cookies/status."""
        total = sum(len(e.cookies) for e in self._jars.values())
        return {
            "domains": len(self._jars),
            "total_cookies": total,
            "source_file": self._source_file,
            "last_loaded_seconds_ago": (
                round(time.monotonic() - self._last_loaded, 1)
                if self._last_loaded else None
            ),
        }

    async def import_from_list(self, domain: str, cookie_list: list[dict]) -> None:
        """Import cookies from FlareSolverr-format cookie list."""
        cookies = {}
        for c in cookie_list:
            name = c.get("name", "")
            value = c.get("value", "")
            if name:
                cookies[name] = value
        if cookies:
            await self.set_cookies(domain, cookies)

    async def export_as_header(self, domain: str) -> str:
        """Export cookies as a Cookie header string."""
        cookies = await self.get_cookies(domain)
        if not cookies:
            return ""
        return "; ".join(f"{k}={v}" for k, v in cookies.items())

    async def clear_domain(self, domain: str) -> None:
        """Clear all cookies for a domain."""
        async with self._lock:
            self._jars.pop(domain, None)

    async def cleanup_expired(self) -> int:
        """Remove all expired domain cookies. Returns count removed."""
        async with self._lock:
            now = time.monotonic()
            expired = [
                d for d, entry in self._jars.items()
                if now - entry.last_updated > entry.ttl
            ]
            for d in expired:
                del self._jars[d]
            return len(expired)

    @property
    def domain_count(self) -> int:
        return len(self._jars)
