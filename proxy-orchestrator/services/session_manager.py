"""
Session Manager — Per-domain persistent sessions with cookie jars.
Sticky session assignment with TTL cleanup.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from config import config
from services.cookie_jar import CookieManager

logger = logging.getLogger(__name__)


@dataclass
class Session:
    """A persistent session for a domain."""
    session_id: str
    domain: str
    created_at: float
    last_used: float
    request_count: int = 0
    user_agent: str = ""
    proxy_url: Optional[str] = None
    metadata: dict = field(default_factory=dict)


class SessionManager:
    """
    Per-domain session management with sticky assignment and TTL cleanup.
    Maintains session state across strategy escalation.
    """

    def __init__(self, cookie_manager: CookieManager) -> None:
        self._sessions: dict[str, Session] = {}  # session_id -> Session
        self._domain_sessions: dict[str, str] = {}  # domain -> session_id
        self._cookie_manager = cookie_manager
        self._ttl = config.SESSION_TTL
        self._lock = asyncio.Lock()

    async def get_or_create(
        self, 
        domain: str, 
        session_id: Optional[str] = None, 
        force_new: bool = False
    ) -> Session:
        """Get existing session for domain or create new one."""
        async with self._lock:
            # 1. If explicit session_id provided, use it
            if session_id:
                if session_id in self._sessions:
                    if force_new:
                        old = self._sessions.pop(session_id, None)
                        if old and self._domain_sessions.get(old.domain) == session_id:
                            del self._domain_sessions[old.domain]
                        await self._cookie_manager.clear_domain(domain)
                    else:
                        session = self._sessions[session_id]
                        if session.domain == domain:
                            session.last_used = time.monotonic()
                            session.request_count += 1
                            return session
                
                # Create new session with this specific ID
                sid = session_id
            
            # 2. Otherwise use domain-based sticky session
            elif domain in self._domain_sessions and not force_new:
                sid = self._domain_sessions[domain]
                if sid in self._sessions:
                    session = self._sessions[sid]
                    if time.monotonic() - session.last_used < self._ttl:
                        session.last_used = time.monotonic()
                        session.request_count += 1
                        return session
            
            elif force_new and domain in self._domain_sessions:
                # Clean up old session inline — avoids deadlock with destroy_session
                sid = self._domain_sessions.pop(domain)
                self._sessions.pop(sid, None)
                await self._cookie_manager.clear_domain(domain)
                logger.debug(f"Forced new session for {domain}. Old {sid} cleared.")
                sid = str(uuid.uuid4())[:12]
            else:
                sid = str(uuid.uuid4())[:12]

            # Create new session object
            now = time.monotonic()
            session = Session(
                session_id=sid,
                domain=domain,
                created_at=now,
                last_used=now,
                request_count=1,
            )
            self._sessions[sid] = session
            # Only update domain mapping if it's the primary session for this domain
            if not session_id or domain not in self._domain_sessions:
                self._domain_sessions[domain] = sid
                
            logger.debug(f"Session {sid} ready for domain {domain}")
            return session

    async def get_session(self, session_id: str) -> Optional[Session]:
        """Get a specific session by ID."""
        async with self._lock:
            return self._sessions.get(session_id)

    async def update_session(
        self,
        session_id: str,
        user_agent: Optional[str] = None,
        proxy_url: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        """Update session properties."""
        async with self._lock:
            session = self._sessions.get(session_id)
            if session:
                if user_agent:
                    session.user_agent = user_agent
                if proxy_url:
                    session.proxy_url = proxy_url
                if metadata:
                    session.metadata.update(metadata)
                session.last_used = time.monotonic()

    async def destroy_session(self, session_id: str) -> None:
        """Destroy a session and clean up its resources."""
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session:
                # Remove domain mapping
                if self._domain_sessions.get(session.domain) == session_id:
                    del self._domain_sessions[session.domain]
                # Clear cookies
                await self._cookie_manager.clear_domain(session.domain)
                logger.debug(f"Session {session_id} destroyed for {session.domain}")

    async def cleanup_expired(self) -> int:
        """Remove all expired sessions. Returns count removed."""
        async with self._lock:
            now = time.monotonic()
            expired = [
                sid for sid, s in self._sessions.items()
                if now - s.last_used > self._ttl
            ]
            for sid in expired:
                session = self._sessions.pop(sid)
                if self._domain_sessions.get(session.domain) == sid:
                    del self._domain_sessions[session.domain]
            if expired:
                logger.info(f"Cleaned up {len(expired)} expired sessions")
            return len(expired)

    @property
    def active_count(self) -> int:
        return len(self._sessions)

    async def get_stats(self) -> dict:
        async with self._lock:
            return {
                "active_sessions": len(self._sessions),
                "domains": list(self._domain_sessions.keys()),
            }
