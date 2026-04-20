"""
Password policy checks for registration and (eventually) password
change flows. Two layers:

    1. Length floor: at least 8 characters. Kept as a minimum so
       operators who want longer can just set a policy constant in
       one place later. Length over complexity is the modern guidance
       — a long passphrase beats a short string of punctuation.

    2. Breach-list check via the HaveIBeenPwned k-anonymity API.
       We SHA-1 the password, send only the first 5 hex characters
       over the wire, and compare the full hash against the returned
       suffix list locally. The server never sees the full password
       or the full hash.

    Fail-open: if HIBP is unreachable or slow, accept the password
    rather than block registration. An attacker who wants a breached
    password still needs to reach our server, which is the actual
    attack surface; blocking on a third-party outage would deny
    service to legit users without a security benefit.

Sync HIBP call is run in a thread via asyncio.to_thread so the event
loop is not blocked during the short HTTP round-trip.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import urllib.error
import urllib.request
from typing import Optional


_log = logging.getLogger("notnative.password")


MIN_PASSWORD_LEN = 8

HIBP_API = "https://api.pwnedpasswords.com/range/"
# Keep the timeout short so a struggling HIBP endpoint does not hold
# up registration for every user on our box.
HIBP_TIMEOUT_SECS = 2.0
HIBP_USER_AGENT = "NotNativeMemory/1.0"


def _sha1_hex_upper(secret: str) -> str:
    return hashlib.sha1(secret.encode("utf-8")).hexdigest().upper()


def _query_hibp_sync(prefix: str) -> Optional[set[str]]:
    """
    Blocking HTTP fetch of the HIBP suffix list for a prefix. Returns
    a set of uppercase hex suffixes on success, or None on any error
    (network, non-200, parse). Call via asyncio.to_thread.
    """
    try:
        req = urllib.request.Request(
            HIBP_API + prefix,
            headers={
                "User-Agent": HIBP_USER_AGENT,
                # Padding tells HIBP to pad the response with randomness
                # so an on-path observer cannot infer which prefix was
                # queried based on response length alone.
                "Add-Padding": "true",
            },
        )
        with urllib.request.urlopen(req, timeout=HIBP_TIMEOUT_SECS) as resp:
            if resp.status != 200:
                return None
            body = resp.read().decode("ascii", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _log.debug("HIBP query failed: %s", exc)
        return None

    suffixes: set[str] = set()
    # Each non-empty line looks like "SUFFIX:COUNT". Count is the number
    # of times the password has been seen in breaches; we ignore it —
    # even one is too many.
    for line in body.splitlines():
        stripped = line.strip()
        if ":" in stripped:
            suffix = stripped.split(":", 1)[0].strip().upper()
            if suffix:
                suffixes.add(suffix)
    return suffixes


async def is_pwned(password: str) -> Optional[bool]:
    """
    Check HIBP for `password`. Returns:
        True   — the password has been seen in a known breach.
        False  — no match in HIBP.
        None   — the query failed (network / timeout / non-200).

    Callers treat None as "fail open" (accept the password).
    """
    if not password:
        return None
    digest = _sha1_hex_upper(password)
    prefix, suffix = digest[:5], digest[5:]
    suffixes = await asyncio.to_thread(_query_hibp_sync, prefix)
    if suffixes is None:
        return None
    return suffix in suffixes


async def validate_new_password(password: str) -> Optional[str]:
    """
    Run policy checks on a proposed new password. Returns None if the
    password passes; returns a user-facing error string otherwise.

    Order matters: cheap local checks first, network check last so a
    password that fails length does not trigger an HIBP call.
    """
    if not password or len(password) < MIN_PASSWORD_LEN:
        return f"password must be at least {MIN_PASSWORD_LEN} characters"

    breached = await is_pwned(password)
    if breached is True:
        return (
            "This password appears in known-breach lists. "
            "Pick a different one — a long passphrase beats a short "
            "string of punctuation."
        )
    # breached False or None (fail-open) → accept.
    return None
