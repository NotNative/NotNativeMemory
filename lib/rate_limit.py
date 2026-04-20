"""
In-memory rate limiter with exponential backoff for authentication
endpoints. Single-process scope: state lives in per-module OrderedDicts
and does not cross process boundaries. Revisit if the server ever runs
multi-worker (consider Redis or similar shared store).

Model:
    Per (bucket, key) we track a consecutive-failure counter and a
    blocked-until timestamp. Each failure advances the blocked-until
    mark by `min(2**(failures-1), MAX_BACKOFF)` seconds: 1, 2, 4, 8, 16,
    ... capped at 5 minutes. A successful flow clears the entry.

    Login rate-limits are keyed by BOTH request IP and username, so an
    attacker cannot trivially bypass by rotating IPs (username bucket
    catches it) or by rotating usernames from one IP (IP bucket catches
    it). Registration is rate-limited by IP only because usernames
    don't exist yet at check time.

Integration:
    Routes call `check_login` / `check_register` at the top of their
    handler; a False return carries a `retry_after` seconds hint that
    should be surfaced to the caller via `Retry-After`. On success the
    login route calls `clear_login` to reset the counter. Registration
    calls `record_register_attempt` on every attempt (success or
    failure) since the attack model is "too many registrations per IP",
    not "too many failures per IP".

Bounds:
    Each bucket is an OrderedDict capped at MAX_ENTRIES entries. On
    insert past the cap we evict the LRU entry. Entries idle for
    ENTRY_TTL seconds are considered stale and discarded on next access.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Tuple


# Backoff doubles on each consecutive failure, up to this ceiling.
MAX_BACKOFF_SECS = 300.0

# Entries untouched for this long are considered stale and effectively
# grant a fresh start. Long enough that a bruteforcer cannot simply wait
# it out for free, short enough that a legit user who mistypes their
# password an hour after their last failure isn't still paying backoff.
ENTRY_TTL_SECS = 3600.0

# Hard cap per bucket to prevent unbounded growth. Stays generous enough
# that real traffic doesn't evict legitimate entries; small enough that
# memory stays bounded against a hostile enumeration attempt.
MAX_ENTRIES = 10_000

# Username keys are truncated before use so a hostile client cannot
# bloat the bucket by submitting megabyte-long usernames. 64 chars fits
# any realistic username; anything longer collapses to the same key,
# which only helps us (same-bucket backoff).
MAX_USERNAME_KEY_LEN = 64


def _now() -> float:
    return time.monotonic()


def _normalize_username(username: str) -> str:
    """Case-insensitive, length-capped username key."""
    return (username or "").strip().lower()[:MAX_USERNAME_KEY_LEN]


def client_ip(request) -> str:
    """
    Best-effort client IP from a Starlette Request. Returns "unknown"
    when the connection's peer is not recoverable (shouldn't happen in
    production TCP flows, but defensive for tests and edge cases).

    Behind a reverse proxy, uvicorn's --forwarded-allow-ips argument
    rewrites request.client from X-Forwarded-For; we intentionally do
    NOT parse that header ourselves here to avoid trusting it when the
    server is run without the uvicorn flag set.
    """
    if getattr(request, "client", None) is None:
        return "unknown"
    return request.client.host or "unknown"


class _Bucket:
    """
    One logical rate-limit group. Safe under the asyncio single-thread
    assumption: check / record / clear each complete without awaiting,
    so no interleaving occurs. No lock required.
    """

    def __init__(self) -> None:
        # Each entry: {"failures": int, "blocked_until": float, "last_update": float}
        self._entries: "OrderedDict[str, dict]" = OrderedDict()

    def check(self, key: str) -> Tuple[bool, float]:
        """
        Returns `(allowed, retry_after_seconds)`. `retry_after` is zero
        when the request is allowed.
        """
        entry = self._entries.get(key)
        if entry is None:
            return True, 0.0
        now = _now()
        if now - entry["last_update"] > ENTRY_TTL_SECS:
            # Stale — forget it and allow.
            del self._entries[key]
            return True, 0.0
        # Move-to-end so LRU eviction targets genuinely cold keys.
        self._entries.move_to_end(key)
        wait = entry["blocked_until"] - now
        if wait > 0:
            return False, wait
        return True, 0.0

    def record_failure(self, key: str) -> None:
        """
        Count one more failure against this key and extend the block.
        If the entry is stale or missing, it starts fresh at failures=1.
        """
        now = _now()
        entry = self._entries.get(key)
        if entry is None or (now - entry["last_update"] > ENTRY_TTL_SECS):
            entry = {"failures": 0, "blocked_until": 0.0, "last_update": now}
            self._entries[key] = entry
        entry["failures"] += 1
        backoff = min(2.0 ** (entry["failures"] - 1), MAX_BACKOFF_SECS)
        entry["blocked_until"] = now + backoff
        entry["last_update"] = now
        self._entries.move_to_end(key)
        # Evict oldest if we've overflowed the cap. Single pass is
        # enough because every insert grows by one.
        while len(self._entries) > MAX_ENTRIES:
            self._entries.popitem(last=False)

    def clear(self, key: str) -> None:
        self._entries.pop(key, None)


# Separate buckets so a hammered login doesn't poison registration or
# vice versa. Login has two buckets because we key on IP AND username.
_login_ip = _Bucket()
_login_user = _Bucket()
_register_ip = _Bucket()


# -- Login ------------------------------------------------------------


def check_login(request_ip: str, username: str) -> Tuple[bool, float]:
    """
    True if login from (ip, username) is allowed right now. Returns
    (allowed, retry_after). When False, caller should 429 with a
    Retry-After header of `retry_after` (rounded up to an integer).
    """
    u_key = _normalize_username(username)
    ip_allowed, ip_wait = _login_ip.check(request_ip)
    u_allowed, u_wait = _login_user.check(u_key)
    if not ip_allowed or not u_allowed:
        return False, max(ip_wait, u_wait)
    return True, 0.0


def record_login_failure(request_ip: str, username: str) -> None:
    """Call after a failed login attempt (bad credentials, missing user)."""
    u_key = _normalize_username(username)
    _login_ip.record_failure(request_ip)
    _login_user.record_failure(u_key)


def clear_login(request_ip: str, username: str) -> None:
    """
    Call after a successful login. Wipes the counters for this IP and
    this username so a legit user's next login doesn't eat a backoff
    from earlier typos.
    """
    u_key = _normalize_username(username)
    _login_ip.clear(request_ip)
    _login_user.clear(u_key)


# -- Registration -----------------------------------------------------


def check_register(request_ip: str) -> Tuple[bool, float]:
    """True if registration from this IP is allowed right now."""
    return _register_ip.check(request_ip)


def record_register_attempt(request_ip: str) -> None:
    """
    Record a registration attempt (success or failure both count).
    Registration doesn't have a clear-on-success because the attack
    model is "too many creations per IP", not "too many failures" —
    a successful registration still advances the counter and slows
    the next attempt from the same source.
    """
    _register_ip.record_failure(request_ip)
