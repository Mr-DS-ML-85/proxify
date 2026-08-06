#!/usr/bin/env python3
"""
TLS-impersonating MITM CONNECT proxy (persona-consistent).

The GUI Chrome's real Chromium 149 JA3 is what Google flags. This proxy
terminates the browser's TLS locally, then re-opens each request to the
target using curl_cffi impersonating the persona fingerprint (chrome146),
along with persona-consistent client hints, headers and real cookies.

Browser leg: plain HTTP/1.1 (invisible to the target). The browser must trust
the injected Root CA (or launch with --ignore-certificate-errors).
"""
import logging
import os
import socket
import ssl
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from time import time
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tls_mitm")

PERSONA_UA = os.getenv(
    "PERSONA_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
)
PERSONA_ACCEPT_LANG = os.getenv("PERSONA_ACCEPT_LANGUAGE", "en-US,en;q=0.9")
PERSONA_VIEWPORT = os.getenv("PERSONA_VIEWPORT", "1920")
PERSONA_PLATFORM = os.getenv("PERSONA_PLATFORM", "Windows").strip('"')
PERSONA_PLATFORM_VERSION = os.getenv("PERSONA_PLATFORM_VERSION", "17.0.0")
PERSONA_UA_HINT_VERSION = "146.0.6943.141"
PERSONA_SEC_CH_UA = '"Not_A Brand";v="24", "Chromium";v="146", "Google Chrome";v="146"'
IMPERSONATE = os.getenv("TLS_IMPERSONATE", "chrome146")
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.getenv("TLS_MITM_PORT", "9445"))
COOKIE_FILE = os.getenv("GUI_COOKIE_FILE", "/app/gui-cookies.txt")

_CA_DIR = "/tmp/tls_mitm_ca"
_CA_KEY = os.path.join(_CA_DIR, "ca.key")
_CA_CERT = os.path.join(_CA_DIR, "ca.crt")
_cert_cache: dict[str, ssl.SSLContext] = {}
_cert_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=12)


def _ensure_ca() -> str:
    import datetime
    if os.path.exists(_CA_CERT):
        return _CA_CERT
    os.makedirs(_CA_DIR, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Proxify MITM Root CA")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    with open(_CA_KEY, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    with open(_CA_CERT, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return _CA_CERT


def _make_ctx(host: str) -> ssl.SSLContext:
    import datetime
    with open(_CA_KEY, "rb") as f:
        ca_key = serialization.load_pem_private_key(f.read(), password=None)
    with open(_CA_CERT, "rb") as f:
        ca_cert = x509.load_pem_x509_certificate(f.read())
    key = ca_key
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(ca_cert.subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=398))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.set_alpn_protocols(["http/1.1"])
    tmp = os.path.join(_CA_DIR, f"leaf-{host.replace(':', '_').replace('/', '_')}.pem")
    try:
        with open(tmp, "w") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM).decode())
            f.write(ca_cert.public_bytes(serialization.Encoding.PEM).decode())
        tmpkey = tmp.replace(".pem", ".key")
        with open(tmpkey, "w") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()).decode())
        ctx.load_cert_chain(certfile=tmp, keyfile=tmpkey)
    finally:
        pass
    return ctx


def _ctx_for(host: str) -> ssl.SSLContext:
    with _cert_lock:
        if host in _cert_cache:
            return _cert_cache[host]
    ctx = _make_ctx(host)
    _cert_cache[host] = ctx
    return ctx


def _load_cookies(host: str):
    if not os.path.exists(COOKIE_FILE):
        return None
    domain = host
    if host.startswith("www."):
        domain = host[4:]
    ok = []
    with open(COOKIE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 7:
                continue
            cdomain, name, value = parts[0], parts[5], parts[6]
            if cdomain in (host, domain, "." + domain):
                # exclude the fragile login/hosted-signout tokens that destabilize
                # an anonymous IP session
                if name not in ("SID", "LSID", "SSID", "APISID", "SAPISID", "HSID",
                                "_GRECAPTCHA", "SEARCH_SAMESITE", "NID", "SNID"):
                    ok.append((name, value))
    return dict(ok) or None


def _persona_headers(h: dict) -> dict:
    drop = {"host", "connection", "proxy-connection", "content-length",
            "cookie", "accept-encoding", "proxy-authorization"}
    drop |= {k for k in h if k.lower().startswith("sec-ch-ua") and k.lower() not in (
        "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform")}
    out = {k: v for k, v in h.items() if k.lower() not in drop and k.lower() not in (
        "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform")}
    out["User-Agent"] = PERSONA_UA
    out["Accept-Language"] = PERSONA_ACCEPT_LANG
    out["Viewport-Width"] = PERSONA_VIEWPORT
    out["Sec-Ch-Ua"] = PERSONA_SEC_CH_UA
    out["Sec-Ch-Ua-Mobile"] = "?0"
    out["Sec-Ch-Ua-Platform"] = '"%s"' % PERSONA_PLATFORM
    out["Sec-CH-UA-Platform-Version"] = '"%s"' % PERSONA_PLATFORM_VERSION
    out["Sec-CH-UA-Full-Version-List"] = '"Chromium";v="146.0.6943.141", "Google Chrome";v="146.0.6943.141", "Not_A Brand";v="24"'
    out["Sec-CH-UA-Full-Version"] = '"146.0.6943.141"'
    out.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7")
    out["Accept-Encoding"] = "gzip, deflate, br"
    out.setdefault("Sec-Fetch-Site", "none")
    out.setdefault("Sec-Fetch-Mode", "navigate")
    out.setdefault("Sec-Fetch-Dest", "document")
    out.setdefault("Sec-Fetch-User", "?1")
    return out


_upstream_sessions_lock = threading.Lock()
_upstream_sessions: dict[str, object] = {}
_UPSTREAM_TTL = 600.0


def _session_for(host: str):
    """Return a persistent per-host upstream session (connection + cookie reuse).

    A real Chrome keeps ONE HTTP/2 (-capable) connection and cookie jar per
    origin. Re-using a curl_cffi Session (instead of a brand-new TLS handshake
    per request) means Google see downstream connection lifecycle + session reuse rather
    than an unfriendly 'new handshake every request' pattern.
    """
    from curl_cffi import requests as cr
    now = time()
    with _upstream_sessions_lock:
        st = _upstream_sessions.get(host)
        if st and (now - st[0]) > _UPSTREAM_TTL:
            _upstream_sessions.pop(host, None)
            st = None
        if not st:
            st = (now, cr.Session(impersonate=IMPERSONATE, timeout=35))
            _upstream_sessions[host] = st
        return st[1]


def _upstream(method: str, url: str, headers: dict, body: bytes | None):
    host = (urlparse(url).hostname or "")
    sess = _session_for(host)
    try:
        return sess.request(method, url, headers=headers, data=body,
                           impersonate=IMPERSONATE, http_version="2",
                           timeout=35, allow_redirects=False,
                           cookies=_load_cookies(host))
    except Exception:
        # Fallback: h2 may be unsupported/refused by the server — retry on
        # http/1.1 rather than failing the request outright.
        return sess.request(method, url, headers=headers, data=body,
                            impersonate=IMPERSONATE, timeout=35,
                            allow_redirects=False, cookies=_load_cookies(host))


def _pack_upstream(resp) -> bytes:
    status = resp.status_code
    phrase = str(HTTPStatus(status).phrase) if status in HTTPStatus._value2member_map_ else "OK"
    out = [f"HTTP/1.1 {status} {phrase}\r\n"]
    for k, v in resp.headers.items():
        if k.lower() in ("transfer-encoding", "connection", "content-length", "content-encoding"):
            continue
        out.append(f"{k}: {v}\r\n")
    body = resp.content
    out.append(f"Content-Length: {len(body)}\r\n\r\n")
    return "".join(out).encode() + body


def _serve_http(conn: socket.socket, host: str):
    conn.settimeout(30)
    buf = b""
    log.info("serve start %s", host)
    try:
        while True:
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            head, _, buf = buf.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            try:
                method, url, _ = lines[0].decode(errors="replace").split(" ", 2)
            except Exception:
                return
            headers = {}
            for ln in lines[1:]:
                if b":" in ln:
                    k, _, v = ln.decode(errors="replace").partition(":")
                    headers[k.strip().lower()] = v.strip()
            body = b""
            cl = headers.get("content-length")
            if cl:
                need = int(cl)
                while len(buf) < need:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf += chunk
                body, buf = buf[:need], buf[need:]
            if url.startswith("/"):
                url = f"https://{host}{url}"
            resp = _upstream(method, url, _persona_headers(headers), body)
            packed = _pack_upstream(resp)
            log.info("served %s %s -> %d (%d bytes)", method, url, resp.status_code, len(packed))
            conn.sendall(packed)
            if headers.get("connection", "").lower() == "close":
                return
    except Exception as e:
        log.warning("serve %s: %s", host, e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _tls_relay(conn: socket.socket, host: str):
    try:
        ctx = _ctx_for(host)
        tls = ctx.wrap_socket(conn, server_side=True)
        _serve_http(tls, host)
    except Exception as e:
        log.warning("tls-relay %s: %s", host, e)
        try:
            conn.close()
        except Exception:
            pass


def handle_conn(conn: socket.socket):
    conn.settimeout(20)
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = conn.recv(4096)
            if not chunk:
                conn.close()
                return
            req += chunk
        lines = req.split(b"\r\n")
        try:
            method, target, _ = lines[0].decode(errors="replace").split(" ", 2)
        except Exception:
            conn.close()
            return
        headers = {}
        for ln in lines[1:]:
            if b":" in ln:
                k, _, v = ln.decode(errors="replace").partition(":")
                headers[k.strip().lower()] = v.strip()
        if method == "CONNECT":
            host, port = target.rsplit(":", 1)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            threading.Thread(target=_tls_relay, args=(conn, host), daemon=True).start()
            return
        else:
            body = b""
            cl = headers.get("content-length")
            if cl:
                body = req.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in req else b""
                while len(body) < int(cl):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    body += chunk
            resp = _upstream(method, target, _persona_headers(headers), body)
            conn.sendall(_pack_upstream(resp))
            conn.close()
    except Exception as e:
        log.warning("conn: %s", e)
        try:
            conn.close()
        except Exception:
            pass


def main():
    _ensure_ca()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((LISTEN_HOST, LISTEN_PORT))
    srv.listen(64)
    log.info("TLS-impersonating MITM on %s:%d (impersonate=%s) CA=%s", LISTEN_HOST, LISTEN_PORT, IMPERSONATE, _CA_CERT)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle_conn, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(2)
            s.connect((LISTEN_HOST, LISTEN_PORT))
            sys.exit(0)
        except Exception:
            sys.exit(1)
        finally:
            s.close()
    main()