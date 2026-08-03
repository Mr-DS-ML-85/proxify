#!/usr/bin/env python3
"""
brave_cookies.py — Extract the user's real Brave session cookies and inject
them into the proxy-orchestrator GUI Chrome (stealth: cookies + cache so
Google/Reddit don't hit captchas).

HOW IT WORKS (reverse-engineered, see BUG_POWER.md SF-14):
  Brave 1.93.x (Chromium ~149, before the June-2026 "Remove kEncryptSyncCompat"
  commit) encrypts its cookie DB with Chromium's PosixKeyProvider:

      blob = "v10"(3) + salt(16) + IV(16) + AES-128-CBC(plaintext, PKCS7)
      key  = fd621fe5a2b402539dfa147ca9272778
           = PBKDF2-HMAC-SHA1("peanuts", "saltysalt", 1 iteration, 16 bytes)
      IV   = blob[19:35]   (random per cookie — MUST be taken from the blob)
      salt = blob[3:19]    (random per cookie — ignore for decryption)

  The key is HARDCODED in Chromium — it is identical on every Linux
  Chromium/Brave install, so no keyring access is needed. The gnome-keyring
  "Application key for brave_brave" portal secret is a red herring (that's the
  v12 portal provider — cookies here are v10).

Usage:
  # 1) Extract to Netscape file (host):
  python3 scripts/brave_cookies.py extract \
      --profile ~/snap/brave/current/.config/BraveSoftware/Brave-Browser \
      --out /tmp/brave_cookies_netscape.txt

  # 2) Copy into container + inject into GUI Chrome over CDP:
  docker cp /tmp/brave_cookies_netscape.txt po-test:/tmp/brave_cookies_netscape.txt
  python3 scripts/brave_cookies.py inject --src /tmp/brave_cookies_netscape.txt \
      --cdp http://127.0.0.1:9222        # (run inside the container)

  # 3) Or do both on the host: inject via the container's CDP through SSH/port.
Deps: cryptography (pip install cryptography)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ── the hardcoded Chromium PosixKeyProvider key (AES-128-CBC) ──────────
V10_KEY = bytes.fromhex("fd621fe5a2b402539dfa147ca9272778")
V10_PREFIX = b"v10"
SALT_LEN = 16
IV_LEN = 16
HEADER_LEN = len(V10_PREFIX) + SALT_LEN + IV_LEN  # 3 + 16 + 16 = 35


def decrypt_cookie_value(blob: bytes) -> bytes | None:
    """Decrypt one v10 cookie blob → plaintext bytes (or None)."""
    if blob[:3] != V10_PREFIX or len(blob) < HEADER_LEN:
        return None
    iv = blob[len(V10_PREFIX) + SALT_LEN:HEADER_LEN]     # blob[19:35]
    ct = blob[HEADER_LEN:]                                # blob[35:]
    dec = Cipher(algorithms.AES(V10_KEY), modes.CBC(iv)).decryptor()
    pt = dec.update(ct) + dec.finalize()
    pad = pt[-1]
    if pad < 1 or pad > 16 or pt[-pad:] != bytes([pad]) * pad:
        return None
    return pt[:-pad]


def extract_cookies(profile_dir: str, wanted: tuple[str, ...] = ("google", "reddit")):
    """
    Read the Cookies SQLite DB under profile_dir, decrypt every v10 cookie,
    return list of dicts with real plaintext values (all domains).
    """
    db_path = os.path.join(profile_dir, "Default", "Cookies")
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"cookie DB not found: {db_path}")

    con = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(cookies)").fetchall()]
        # select the intersection of known columns and what this schema has
        want = [c for c in (
            "host_key", "name", "encrypted_value", "path", "expires_utc",
            "is_secure", "is_httponly", "is_same_party", "samesite",
        ) if c in cols]
        rows = con.execute(
            "SELECT %s FROM cookies ORDER BY length(encrypted_value) DESC"
            % ",".join(want)
        ).fetchall()
    finally:
        con.close()

    idx = {c: i for i, c in enumerate(want)}
    g = idx.get

    cookies = []
    total = 0
    for row in rows:
        blob = row[g("encrypted_value")]
        if blob[:3] != V10_PREFIX:
            continue
        total += 1
        pt = decrypt_cookie_value(blob)
        if pt is None:
            continue
        try:
            value = pt.decode("utf-8")
        except UnicodeDecodeError:
            value = pt.decode("utf-8", "replace")
        if not value:
            continue
        cookies.append({
            "host": row[g("host_key")],
            "name": row[g("name")],
            "value": value,
            "path": (row[g("path")] or "/") if g("path") is not None else "/",
            "expires_utc": (row[g("expires_utc")] or 2147483648 * 1_000_000),
            "secure": bool(row[g("is_secure")]) if g("is_secure") is not None else False,
            "httponly": bool(row[g("is_httponly")]) if g("is_httponly") is not None else False,
        })
    print(f"[brave_cookies] v10 blobs: {total}, cleanly decrypted: "
          f"{len(cookies)}", file=sys.stderr)
    return cookies


def write_netscape(cookies, out_path: str, wanted: tuple[str, ...] | None = None):
    """Write cookies to a Netscape-format cookie file."""
    lines = ["# Netscape HTTP Cookie File",
             "# Extracted by proxy-orchestrator/scripts/brave_cookies.py",
             "# scheme: Brave 1.93 v10 sync-compat (BUG_POWER.md SF-14)"]
    n = 0
    for c in cookies:
        if wanted and not any(w in c["host"] for w in wanted):
            continue
        domain = c["host"] if c["host"].startswith(".") else "." + c["host"]
        lines.append("%s\tTRUE\t%s\t%s\t%d\t%s\t%s" % (
            domain, c["path"],
            "TRUE" if c["secure"] else "FALSE",
            c["expires_utc"] // 1_000_000,
            c["name"], c["value"]))
        n += 1
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[brave_cookies] wrote {n} cookies -> {out_path}")
    return n


def parse_netscape(path: str) -> list[dict]:
    """Parse a Netscape cookie file into Playwright add_cookies dicts."""
    cookies = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                domain, _, path_, secure, expires, name, value = parts[:7]
                if not name or not value:
                    continue
                try:
                    exp = int(expires)
                except (ValueError, TypeError):
                    exp = 2147483647
                c = {
                    "domain": domain, "path": path_ or "/",
                    "secure": secure == "TRUE", "expires": exp,
                    "name": name, "value": value,
                }
                # __Host- cookies MUST be host-only (no Domain attribute),
                # sent only over HTTPS, and only on the exact host.
                if name.startswith("__Host-"):
                    c.pop("domain", None)
                    c.pop("path", None)
                    c["secure"] = True
                    c["url"] = "https://" + domain.lstrip(".") + "/"
                cookies.append(c)
    return cookies


async def inject_via_cdp(src: str, cdp_url: str) -> None:
    """Inject cookies into the GUI Chrome's persistent default context over CDP."""
    from playwright.async_api import async_playwright

    cookies = parse_netscape(src)
    print(f"[brave_cookies] injecting {len(cookies)} cookies via {cdp_url}")

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url, timeout=15_000)
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("GUI Chrome has no persistent context (incognito?)")
        context = contexts[0]
        ok, failed = 0, []
        for c in cookies:
            try:
                await context.add_cookies([c])
                ok += 1
            except Exception as e:
                failed.append((c.get("domain", c.get("url", "?")),
                               c["name"], str(e)[:60]))
        print(f"[brave_cookies] added {ok}/{len(cookies)}")
        for f_ in failed[:10]:
            print("   failed:", f_)

        # verify persistence via the running browser's own cookie store
        got = await context.cookies()
        google = [c for c in got if "google" in c.get("domain", "")]
        reddit = [c for c in got if "reddit" in c.get("domain", "")]
        print(f"[brave_cookies] context now: total={len(got)} google={len(google)} "
              f"reddit={len(reddit)}")
        for c in (google + reddit)[:6]:
            print("   %-24s %-20s = %s" % (
                c["domain"], c["name"], c["value"][:40]))
        await browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Brave 1.93 cookie extractor/injector")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("extract", help="decrypt Brave cookie DB -> Netscape file")
    ex.add_argument("--profile", default=os.path.expanduser(
        "~/snap/brave/current/.config/BraveSoftware/Brave-Browser"),
        help="Brave profile dir (contains Default/Cookies)")
    ex.add_argument("--out", default="/tmp/brave_cookies_netscape.txt")
    ex.add_argument("--all", action="store_true",
                    help="export every domain (default: google+reddit+yandex only)")
    ex.add_argument("--wanted", default="google,reddit,yandex",
                    help="comma-separated substrings to filter cookie hosts")
    ex.set_defaults(func=cmd_extract)

    inj = sub.add_parser("inject", help="inject Netscape file into GUI Chrome via CDP")
    inj.add_argument("--src", default="/tmp/brave_cookies_netscape.txt")
    inj.add_argument("--cdp", default="http://127.0.0.1:9222")
    inj.set_defaults(func=cmd_inject)

    args = ap.parse_args()
    args.func(args)


def cmd_extract(args) -> None:
    cookies = extract_cookies(args.profile)
    wanted = None if args.all else tuple(
        w.strip() for w in args.wanted.split(",") if w.strip()
    )
    n = write_netscape(cookies, args.out, wanted)
    if n == 0:
        print("WARNING: no cookies matched — check --profile path or use --all",
              file=sys.stderr)
        sys.exit(1)


def cmd_inject(args) -> None:
    if not os.path.exists(args.src):
        print(f"missing source file: {args.src}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(inject_via_cdp(args.src, args.cdp))


if __name__ == "__main__":
    main()
