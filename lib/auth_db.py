"""
Database operations for auth (users + bearer tokens).

Split from lib/db.py so the memory layer stays focused on memories.
All functions here take or return the hashed form of tokens and
passwords. Raw plaintext never lives longer than the HTTP request
that carried it.

Depends on lib.db.get_pool() for the shared asyncpg connection pool.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from lib import auth
from lib.db import get_pool
from lib.limits import (
    MAX_PASSWORD_BYTES,
    MAX_USERNAME_BYTES,
    PayloadTooLarge,
    enforce_field_len,
)


_log = logging.getLogger("notnative.auth")


# ==========================================================================
# Users
# ==========================================================================


async def count_users() -> int:
    """Return total number of users. Handy for deciding first-run setup."""
    pool = await get_pool()
    row = await pool.fetchrow("SELECT COUNT(*) AS n FROM users")
    return int(row["n"] or 0)


async def create_user(username: str, password: str) -> Dict[str, Any]:
    """
    Create a user. Raises asyncpg.UniqueViolationError if the username
    is taken. Hashes the password with scrypt before writing.

    No admin concept anymore. Every user is equal; each sees only their
    own memories.
    """
    if not username or not username.strip():
        raise ValueError("username is required")
    if not password or len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    # Per-field caps before we spend scrypt time hashing a megabyte
    # "password". PayloadTooLarge is a ValueError subclass so callers
    # already-handling ValueError keep working.
    try:
        enforce_field_len(username.strip(), MAX_USERNAME_BYTES, "username")
        enforce_field_len(password, MAX_PASSWORD_BYTES, "password")
    except PayloadTooLarge as exc:
        raise ValueError(str(exc))

    hashed = auth.hash_secret(password)
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO users (username, password_hash)
        VALUES ($1, $2)
        RETURNING id, username, created_at
        """,
        username.strip(), hashed,
    )
    return _row_to_user(row)


async def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """
    Lookup a user by username. Returns dict INCLUDING password_hash
    for login verification, or None if no such user.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, username, password_hash, created_at
        FROM users WHERE username = $1
        """,
        username.strip() if username else "",
    )
    if not row:
        return None
    return dict(row)


async def get_user_by_id(user_id: UUID) -> Optional[Dict[str, Any]]:
    """Lookup a user by ID. Omits password_hash from the returned dict."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, username, created_at FROM users WHERE id = $1
        """,
        user_id,
    )
    return _row_to_user(row) if row else None


def _row_to_user(row) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "username": row["username"],
        "created_at": row["created_at"].isoformat(),
    }


# ==========================================================================
# Tokens
# ==========================================================================


async def create_token(
    user_id: UUID, label: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate, hash, and store a new Bearer token for this user.

    The user's current `token_generation` is snapshotted onto the new
    token's `issued_generation` via subquery, so the mint is atomic
    with the generation read. Tokens minted with an older generation
    than the user's current one are rejected at auth time.

    Returns a dict containing the RAW token value (shown once) plus
    metadata. Callers must pass the raw `token` back to the user and
    never log it.

    The raw token has format nnm_<lookup_key>.<secret>. The lookup
    key is stored plain (indexed for O(1) resolve). The secret is
    scrypt-hashed. See lib/auth.py for the split rationale.
    """
    raw = auth.generate_token()
    parts = auth.parse_token(raw)
    # parse_token returns None only if generate_token's own output is
    # malformed, which would be a logic bug. Assert rather than swallow.
    assert parts is not None, "generate_token produced malformed token"
    lookup_key, secret = parts
    secret_hash = auth.hash_secret(secret)

    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO auth_tokens (
            user_id, token_hash, lookup_key, label, issued_generation
        )
        VALUES (
            $1, $2, $3, $4,
            (SELECT token_generation FROM users WHERE id = $1)
        )
        RETURNING id, user_id, label, created_at
        """,
        user_id, secret_hash, lookup_key, label,
    )
    return {
        "id": str(row["id"]),
        "user_id": str(row["user_id"]),
        "label": row["label"],
        "created_at": row["created_at"].isoformat(),
        "token": raw,  # exposed ONLY here, never again
    }


async def list_tokens(user_id: UUID) -> List[Dict[str, Any]]:
    """List a user's tokens (hashes never returned, only metadata)."""
    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT id, label, created_at, last_used_at, revoked_at
        FROM auth_tokens
        WHERE user_id = $1
        ORDER BY created_at DESC
        """,
        user_id,
    )
    out = []
    for row in rows:
        out.append({
            "id": str(row["id"]),
            "label": row["label"],
            "created_at": row["created_at"].isoformat(),
            "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
            "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None,
        })
    return out


async def revoke_token(user_id: UUID, token_id: UUID) -> bool:
    """
    Mark a token as revoked. Only the token's owner can revoke it;
    revoking someone else's token returns False without side effects.
    """
    pool = await get_pool()
    result = await pool.execute(
        """
        UPDATE auth_tokens
        SET revoked_at = now()
        WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL
        """,
        token_id, user_id,
    )
    return result.endswith(" 1")


async def bump_token_generation(user_id: UUID) -> int:
    """
    Increment a user's token_generation counter by one and return the
    new value. Every outstanding token for this user becomes stale
    immediately (next auth check fails and the client must re-login).

    Intended callers:
      - Admin "force log out this user" lever.
      - Password-change flow (invalidate old sessions on credential
        rotation — to be wired up when password change ships).
      - Incident response (operator loops over users and bumps each,
        or runs a single UPDATE statement directly).

    Idempotent in the sense that each call advances by exactly one.
    Callers do not need to know the prior value.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        UPDATE users
        SET token_generation = token_generation + 1
        WHERE id = $1
        RETURNING token_generation
        """,
        user_id,
    )
    if row is None:
        raise ValueError(f"no such user: {user_id}")
    return int(row["token_generation"])


async def resolve_token(raw_token: str) -> Optional[Dict[str, Any]]:
    """
    Look up an active (non-revoked, non-stale) token by its raw string
    and return the owning user. Returns None if the token is invalid,
    revoked, drifted from the user's current generation, or does not
    match any row.

    O(1) lookup: parse the raw token into (lookup_key, secret), SELECT
    WHERE lookup_key = $1, then scrypt-verify the secret against the
    stored hash. No per-row scrypt loop, no LIMIT window.

    The `t.issued_generation = u.token_generation` predicate enforces
    session revocation: when a user's generation is bumped, every
    previously-issued token is filtered out here and stops passing
    auth immediately.
    """
    parts = auth.parse_token(raw_token)
    if parts is None:
        return None
    lookup_key, secret = parts

    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT t.id, t.token_hash, t.user_id,
               u.username
        FROM auth_tokens t
        JOIN users u ON u.id = t.user_id
        WHERE t.lookup_key = $1
          AND t.revoked_at IS NULL
          AND t.issued_generation = u.token_generation
        """,
        lookup_key,
    )
    if row is None:
        return None

    if not auth.verify_secret(secret, row["token_hash"]):
        return None

    # Fire-and-forget last_used_at update. If it races with another
    # concurrent auth the difference is not functional.
    await pool.execute(
        "UPDATE auth_tokens SET last_used_at = now() WHERE id = $1",
        row["id"],
    )
    return {
        "token_id": str(row["id"]),
        "user_id": row["user_id"],
        "username": row["username"],
    }
