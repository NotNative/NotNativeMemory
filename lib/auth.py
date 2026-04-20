"""
NotNativeMemory - auth primitives

Pure-Python cryptographic helpers with no database or HTTP dependencies.
The rest of the auth layer (DB rows, middleware, routes) lives in
lib/auth_db.py, lib/auth_middleware.py, and server.py. Keeping the
primitives in their own module lets them be unit-tested without a
database or a running server.

Design:
    - Passwords and Bearer tokens both use hashlib.scrypt for hashing
      at rest. scrypt is in the stdlib (no new dep), memory-hard, and
      adequate for a single-user to small-team personal MCP.
    - Raw tokens are generated with secrets.token_urlsafe for 256 bits
      of entropy. The raw value is returned to the caller exactly once
      and only the hash goes to the database.
    - Hashes are stored as `scrypt$N$r$p$saltb64$digestb64` so the
      salt and cost parameters travel with the digest. Cost params
      can be raised later without re-hashing existing users (old
      hashes keep verifying with their original params).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from typing import Optional

# scrypt cost parameters. OWASP 2024 guidance calls for N>=2**17 for
# interactive logins. We pick 2**15 because the single-user server
# runs on hardware as small as a Raspberry Pi and the attack model is
# a trusted home network, not an internet-exposed login. Override by
# setting higher values in a wrapper if your threat model is harsher.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 64
_SALT_BYTES = 16

# OpenSSL's default scrypt maxmem is 32 MiB which is tighter than the
# 128 * N * r = 32 MiB our params need (and too tight once N rises).
# Pass a higher ceiling so the kernel gets to decide the real limit.
_SCRYPT_MAXMEM = 128 * 1024 * 1024

# Token entropy: 32 bytes = 256 bits, URL-safe base64 for clean display.
# The raw token carries a "nnm_" prefix so it is visually distinguishable
# from API keys of other services when it leaks into logs.
_TOKEN_PREFIX = "nnm_"
_TOKEN_BYTES = 32


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(encoded: str) -> bytes:
    # Re-pad because urlsafe_b64encode with rstrip drops the padding.
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


def hash_secret(secret: str) -> str:
    """
    Hash a password or token for at-rest storage.

    Returns:
        A string `scrypt$N$r$p$saltb64$digestb64`. Store the whole
        string in the DB; verify_secret parses the params back out.
    """
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be a non-empty string")

    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=_SCRYPT_DKLEN,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${_b64(salt)}${_b64(digest)}"
    )


def verify_secret(secret: str, stored: str) -> bool:
    """
    Verify a plaintext secret against a hash produced by hash_secret.

    Returns False on any parse error (malformed stored value, wrong
    algorithm, etc.) rather than raising. Callers treat False as "not
    a match" without having to catch exceptions.

    Uses hmac.compare_digest for constant-time comparison so a stolen
    DB dump cannot be mined for near-matches via timing.
    """
    if not isinstance(secret, str) or not secret or not stored:
        return False

    parts = stored.split("$")
    # Format: scrypt$N$r$p$salt$digest  -> 6 segments.
    if len(parts) != 6 or parts[0] != "scrypt":
        return False

    try:
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = _unb64(parts[4])
        expected = _unb64(parts[5])
    except (ValueError, TypeError):
        return False

    try:
        actual = hashlib.scrypt(
            secret.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=_SCRYPT_MAXMEM,
            dklen=len(expected),
        )
    except ValueError:
        # scrypt raises ValueError if params are out of range. Treat
        # that as "not a match" rather than propagating.
        return False

    return hmac.compare_digest(actual, expected)


def generate_token() -> str:
    """
    Return a fresh Bearer token. The prefix `nnm_` is part of the
    token value and must be sent back verbatim in the Authorization
    header. The random segment is 256 bits of entropy, URL-safe.

    The returned value is never stored anywhere by this function. The
    caller MUST hash it with hash_secret before writing to the DB and
    show the raw value to the user exactly once.
    """
    return _TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)


def is_token_shaped(candidate: str) -> bool:
    """
    Cheap pre-flight check before hashing an incoming header. Reject
    obviously-not-our-tokens early so the middleware does not spend
    scrypt time on random garbage.
    """
    return bool(
        isinstance(candidate, str)
        and candidate.startswith(_TOKEN_PREFIX)
        and len(candidate) > len(_TOKEN_PREFIX) + 16
    )


# A fixed scrypt hash computed once at module load, used by
# verify_or_dummy when no real user exists to verify against. Generated
# with the same cost parameters as live user hashes so it takes the
# same wall time. The plaintext is irrelevant — this hash will never
# verify against any password a caller would submit by accident, and
# secrets.compare_digest is constant-time regardless.
_DUMMY_VERIFY_HASH = hash_secret("nnm-dummy-verify-placeholder-" + _b64(secrets.token_bytes(16)))


def verify_or_dummy(password: str, stored_hash: Optional[str]) -> bool:
    """
    Timing-equalized password verify for the login path.

    When `stored_hash` is None (the submitted username did not match a
    user), we still run scrypt against a fixed dummy hash so total
    handler wall time does not distinguish "no such user" from "bad
    password". Both branches then return False via the same scrypt
    compare path; response time leaks nothing about username existence.

    When `stored_hash` is provided, behaves exactly like verify_secret.

    Callers must NOT short-circuit to "no such user" before reaching
    this function; the whole point is that the timing path goes through
    scrypt regardless of user existence.
    """
    if stored_hash is None:
        # Discard the result; always False for a missing user.
        verify_secret(password, _DUMMY_VERIFY_HASH)
        return False
    return verify_secret(password, stored_hash)
