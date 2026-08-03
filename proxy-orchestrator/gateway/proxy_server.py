"""
HTTP/HTTPS Proxy Server — Raw asyncio proxy for SearXNG, scripts, and other apps.
Intercepts requests and routes them through the Decision Engine.

Connection Racing (Happy Eyeballs v2):
  Resolves all IPs (A + AAAA) for the target host and races connections
  across them with staggered 200ms delays. Cancels pending connections
  as soon as one succeeds. Caches working/degraded IPs per domain.
"""

import os
from dotenv import load_dotenv

# Load environment variables early
load_dotenv()
import asyncio
import logging
import socket
import time
from typing import Optional
from urllib.parse import urlparse

from engine.decision_engine import DecisionEngine
from strategies.base import FetchRequest

logger = logging.getLogger(__name__)

# ── IP Cache for Connection Racing ───────────────────────────────────────────
# Maps hostname -> list of (ip, family, last_success_epoch, failure_count)
_IP_CACHE: dict[str, list[tuple[str, int, float, int]]] = {}
_IP_CACHE_TTL = 600  # 10 minutes before re-checking a degraded IP


def _update_ip_cache(host: str, ip: str, family: int, success: bool) -> None:
    """Track which IPs work/fail for each hostname."""
    if host not in _IP_CACHE:
        _IP_CACHE[host] = []
    entries = _IP_CACHE[host]
    for i, (eip, efam, _, efail) in enumerate(entries):
        if eip == ip and efam == family:
            if success:
                entries[i] = (ip, family, time.time(), 0)
            else:
                entries[i] = (ip, family, 0.0, efail + 1)
            return
    # New IP
    entries.append((ip, family, time.time() if success else 0.0, 0 if success else 1))


def _get_working_ips(host: str) -> list[tuple[str, int]]:
    """Return IPs that have succeeded recently, excluding degraded ones."""
    now = time.time()
    entries = _IP_CACHE.get(host, [])
    working = []
    for ip, family, last_ok, fails in entries:
        if fails >= 2 and (now - last_ok) > _IP_CACHE_TTL:
            # Reset after TTL
            pass
        elif fails >= 2:
            continue  # Skip degraded IP
        working.append((ip, family))
    return working


class ProxyServer:
    """
    Async HTTP/HTTPS forward proxy with Connection Racing.

    SearXNG and other apps connect here — no modifications needed.
    Just set:
        outgoing:
          proxies:
            http: http://proxy-orchestrator:8888
            https: http://proxy-orchestrator:8888

    Architecture:
      HTTP  → DecisionEngine pipeline (anti-bot strategies)
      HTTPS → Connection Racing TCP tunnel (Happy Eyeballs v2)

    Connection Racing resolves ALL IPs for the target host and races
    staggered connection attempts (200ms interval). When one succeeds,
    all pending attempts are cancelled. Working IPs are cached per domain.
    """

    def __init__(self, engine: DecisionEngine, host: str = "0.0.0.0", port: int = 8888) -> None:
        self._engine = engine
        self._host = host
        self._port = port
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        """Start the proxy server."""
        self._server = await asyncio.start_server(
            self._handle_connection,
            self._host,
            self._port,
        )
        logger.info(f"Proxy server listening on {self._host}:{self._port}")

    async def stop(self) -> None:
        """Stop the proxy server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Proxy server stopped")

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an incoming proxy connection."""
        try:
            # Read the first line to determine the request type
            first_line = await asyncio.wait_for(reader.readline(), timeout=30.0)
            if not first_line:
                writer.close()
                return

            first_line_str = first_line.decode("utf-8", errors="replace").strip()
            parts = first_line_str.split(" ")
            if len(parts) < 3:
                writer.close()
                return

            method = parts[0].upper()

            if method == "CONNECT":
                await self._handle_connect(parts[1], reader, writer)
            else:
                await self._handle_http(method, parts[1], reader, writer, first_line)

        except asyncio.TimeoutError:
            pass
        except ConnectionResetError:
            pass
        except Exception as e:
            logger.debug(f"Proxy connection error: {e}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_http(
        self,
        method: str,
        url: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        first_line: bytes,
    ) -> None:
        """Handle a plain HTTP proxy request."""
        # Read headers
        headers = {}
        raw_headers = b""
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10.0)
            raw_headers += line
            if line == b"\r\n" or line == b"\n" or not line:
                break
            try:
                header_str = line.decode("utf-8", errors="replace").strip()
                if ":" in header_str:
                    key, value = header_str.split(":", 1)
                    headers[key.strip()] = value.strip()
            except Exception:
                pass

        # Read body if Content-Length is present (case-insensitive)
        body = None
        content_length = next((v for k, v in headers.items() if k.lower() == "content-length"), None)
        if content_length:
            try:
                body = await asyncio.wait_for(
                    reader.read(int(content_length)), timeout=10.0
                )
            except Exception:
                pass

        # Remove proxy-specific headers
        headers.pop("Proxy-Connection", None)
        headers.pop("proxy-connection", None)
        bypass_cache = headers.pop("X-Cache-Bypass", "").lower() == "true"
        force_strategy = headers.pop("X-Force-Strategy", None)
        session_id = headers.pop("X-Session-ID", None)
        force_new_session = headers.pop("X-Force-Session", "").lower() == "true"

        # Create fetch request
        fetch_request = FetchRequest(
            url=url,
            method=method,
            headers=headers,
            body=body,
            bypass_cache=bypass_cache,
            force_strategy=force_strategy,
            session_id=session_id,
            force_new_session=force_new_session,
        )

        # Route through decision engine
        result = await self._engine.fetch(fetch_request)

        # Build HTTP response
        await self._send_response(writer, result)

    async def _resolve_candidates(
        self, host: str, port: int
    ) -> list[tuple[str, int, int]]:
        """
        Resolve all IP candidates (A + AAAA) for a hostname.

        Returns list of (ip, port, family) tuples, interleaving
        IPv4 and IPv6 for Happy Eyeballs compliance.
        Prioritizes IPs from the cache that have worked before.
        """
        candidates: list[tuple[str, int, int]] = []
        seen: set[str] = set()

        # 1. Start with cached working IPs (fast path)
        cached = _get_working_ips(host)
        for ip, family in cached:
            candidates.append((ip, port, family))
            seen.add(ip)

        # 2. Fresh DNS resolution (loop.getaddrinfo for Python 3.10 compat)
        try:
            loop = asyncio.get_running_loop()
            addrinfo = await asyncio.wait_for(
                loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
                timeout=5.0,
            )
            # Interleave v4 and v6 for Happy Eyeballs
            v4s, v6s = [], []
            for fam, _, _, _, sockaddr in addrinfo:
                ip = sockaddr[0]
                if ip in seen:
                    continue
                seen.add(ip)
                if fam == socket.AF_INET6:
                    v6s.append((ip, port, fam))
                else:
                    v4s.append((ip, port, fam))
            # Interleave: v4, v6, v4, v6, ...
            i = 0
            while i < max(len(v4s), len(v6s)):
                if i < len(v4s):
                    candidates.append(v4s[i])
                if i < len(v6s):
                    candidates.append(v6s[i])
                i += 1
        except Exception as e:
            logger.debug(f"DNS resolution failed for {host}: {e}")

        # Remove duplicate IPs while preserving order
        final: list[tuple[str, int, int]] = []
        seen_ips: set[str] = set()
        for ip, p, fam in candidates:
            if ip not in seen_ips:
                seen_ips.add(ip)
                final.append((ip, p, fam))

        if not final:
            logger.warning(f"No IP candidates found for {host}")

        return final

    async def _race_connect(
        self, candidates: list[tuple[str, int, int]], host: str, connect_timeout: float = 10.0
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str, int]:
        """
        Race connection attempts across IP candidates with staggered 200ms delays.

        Happy Eyeballs v2 (RFC 8305):
          - Starts first connection immediately
          - Every 200ms, starts the next candidate
          - When one succeeds, all pending are cancelled
          - Returns (reader, writer, ip, family)
        """
        if not candidates:
            raise OSError("No IP candidates available")

        first_ip = candidates[0][0]
        logger.info(f"Racing {len(candidates)} IPs for {host} (first: {first_ip})")

        pending: set[asyncio.Task] = set()
        winner_event = asyncio.Event()
        winner: tuple[asyncio.StreamReader, asyncio.StreamWriter, str, int] | None = None
        lock = asyncio.Lock()

        async def _try_connect(ip: str, fam: int, delay: float) -> None:
            """Try connecting to a single IP, with optional delay."""
            nonlocal winner
            if delay > 0:
                try:
                    await asyncio.wait_for(winner_event.wait(), timeout=delay)
                    # Another connection won — cancel this attempt
                    return
                except asyncio.TimeoutError:
                    pass

            if winner_event.is_set():
                return

            try:
                r, w = await asyncio.wait_for(
                    asyncio.open_connection(ip, candidates[0][1], family=fam),
                    timeout=connect_timeout,
                )
                async with lock:
                    if not winner_event.is_set():
                        winner = (r, w, ip, fam)
                        winner_event.set()
                        logger.info(f"Tunnel connected via {ip} for {host}")
                    else:
                        # We won the race but another already won — close
                        try:
                            w.close()
                            await w.wait_closed()
                        except Exception:
                            pass
            except (OSError, asyncio.TimeoutError) as e:
                logger.debug(f"Tunnel candidate {ip} failed: {e}")
                _update_ip_cache(host, ip, fam, success=False)

        # Start connection attempts with staggered delays
        for i, (ip, _, fam) in enumerate(candidates):
            delay = i * 0.2  # 200ms stagger
            task = asyncio.create_task(_try_connect(ip, fam, delay))
            pending.add(task)
            task.add_done_callback(pending.discard)

            # If we've started enough, wait a bit for results
            if i >= 1:
                try:
                    await asyncio.wait_for(winner_event.wait(), timeout=0.25)
                    break  # Got a winner
                except asyncio.TimeoutError:
                    continue

        # Wait for a winner or all to complete
        if not winner_event.is_set():
            await asyncio.wait_for(winner_event.wait(), timeout=connect_timeout + 2.0)

        # Cancel all pending tasks
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        if winner is None:
            raise OSError(f"All {len(candidates)} connection candidates failed for {host}")

        r, w, ip, fam = winner
        return r, w, ip, fam

    async def _handle_connect(
        self,
        host_port: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Handle HTTPS CONNECT method.

        Uses Connection Racing (Happy Eyeballs v2):
          1. Resolves all IPs (A + AAAA) for the target host
          2. Races connections with staggered 200ms delays
          3. Cancels pending connections when one succeeds
          4. Caches working/degraded IPs per domain
          5. TLS handshake happens transparently through the tunnel
        """
        # Parse host and port
        if ":" in host_port:
            host, port_str = host_port.rsplit(":", 1)
            port = int(port_str)
        else:
            host = host_port
            port = 443

        remote_reader = None
        remote_writer = None
        connected_ip = ""
        try:
            # Consume remaining headers from CONNECT request
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10.0)
                if line == b"\r\n" or line == b"\n" or not line:
                    break

            # Resolve all IP candidates
            candidates = await self._resolve_candidates(host, port)

            if not candidates:
                raise OSError(f"No IP addresses resolved for {host}")

            # Race with staggered 200ms delays
            remote_reader, remote_writer, connected_ip, connected_fam = await self._race_connect(
                candidates, host, connect_timeout=10.0,
            )

            # Mark as successful (use actual family from winner)
            _update_ip_cache(host, connected_ip, connected_fam, success=True)

            # Send 200 Connection Established to client
            writer.write(b"HTTP/1.1 200 Connection Established\r\n")
            writer.write(f"X-Proxy-IP: {connected_ip}\r\n".encode())
            writer.write(b"\r\n")
            await writer.drain()

            # Pipe bidirectionally — raw encrypted bytes flow through
            await asyncio.gather(
                self._pipe(reader, remote_writer),
                self._pipe(remote_reader, writer),
            )
        except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
            logger.debug(f"Tunnel to {host}:{port} failed: {e}")
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n")
                writer.write(b"Content-Type: application/json\r\n")
                err = f'{{"error":"tunnel_failed","host":"{host}","detail":"{e}"}}'
                writer.write(f"Content-Length: {len(err)}\r\n".encode())
                writer.write(b"\r\n")
                writer.write(err.encode())
                await writer.drain()
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"Tunnel error for {host}:{port}: {e}")
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            # Close remote connection (not the client writer — that's handled by _handle_connection)
            if remote_writer:
                try:
                    remote_writer.close()
                    await remote_writer.wait_closed()
                except Exception:
                    pass

    async def _pipe(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Pipe data from reader to writer (bidirectional tunnel)."""
        try:
            while True:
                data = await asyncio.wait_for(reader.read(65536), timeout=120.0)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _send_response(
        self,
        writer: asyncio.StreamWriter,
        result,
    ) -> None:
        """Send the fetch result as an HTTP response."""
        status_code = result.status_code or 502
        status_text = {
            200: "OK", 301: "Moved Permanently", 302: "Found",
            400: "Bad Request", 403: "Forbidden", 404: "Not Found",
            429: "Too Many Requests", 500: "Internal Server Error",
            502: "Bad Gateway", 503: "Service Unavailable",
        }.get(status_code, "Unknown")

        # Un-wrap Chromium JSON wrapper if present (SearXNG/JSON APIs via Scrapling/FlareSolverr)
        if result.html and result.html.strip().lower().startswith("<html"):
            is_chromium_json = "color-scheme" in result.html.lower() and "<pre" in result.html.lower()
            if is_chromium_json:
                import re
                match = re.search(r'<pre[^>]*>(.*?)</pre>', result.html.strip(), re.IGNORECASE | re.DOTALL)
                if match:
                    inner_text = match.group(1).strip()
                    import html as html_lib
                    inner_text = html_lib.unescape(inner_text)
                    if inner_text.startswith("{") or inner_text.startswith("["):
                        result.html = inner_text

        body = result.html.encode("utf-8") if result.html else result.body or b""
        
        # Prevent SearXNG from vomiting JSONDecodeErrors if Scrapling/FS completely failed or gave us an HTML block page
        # Prevent SearXNG from vomiting JSONDecodeErrors if Scrapling/FS completely failed or gave us an HTML block page
        if (result.is_blocked or not result.success) and (not body or len(body) < 100):
            status_code = 403 if result.is_blocked else 502
            status_text = "Forbidden" if result.is_blocked else "Bad Gateway"
            body = b'{"error": "proxy_timeout_or_blocked", "status": "failed", "strategy": "' + result.strategy_used.encode() + b'"}'
            result.headers["content-type"] = "application/json"
            result.headers.pop("Content-Type", None)
        elif not result.success:
            # If we have a block page with content, still use a 403/502 status but keep the body
            status_code = 403 if result.is_blocked else 502
            status_text = "Forbidden" if result.is_blocked else "Error"

        # Build response headers
        response_lines = [f"HTTP/1.1 {status_code} {status_text}\r\n"]

        # Forward original response headers
        header_skip = {"transfer-encoding", "connection", "content-length", "content-encoding"}
        has_content_type = False
        for key, value in result.headers.items():
            if key.lower() not in header_skip:
                response_lines.append(f"{key}: {value}\r\n")
            if key.lower() == "content-type":
                has_content_type = True
                
        if not has_content_type:
            response_lines.append("Content-Type: text/html; charset=utf-8\r\n")

        # Add our own headers
        response_lines.append(f"Content-Length: {len(body)}\r\n")
        response_lines.append(f"X-Proxy-Strategy: {result.strategy_used}\r\n")
        response_lines.append(f"X-Proxy-Latency: {result.latency:.3f}\r\n")
        if result.cached:
            response_lines.append("X-Proxy-Cache: HIT\r\n")
        response_lines.append("Connection: close\r\n")
        response_lines.append("\r\n")

        header_bytes = "".join(response_lines).encode("utf-8")
        writer.write(header_bytes + body)
        await writer.drain()
