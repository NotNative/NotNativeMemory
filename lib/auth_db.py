"""
Database operations for auth (users + bearer tokens).

Split from lib/db.py so the memory layer stays focused on memories.
All functions here take or return the hashed form of tokens and
passwords. Raw plaintext never lives longer than the HTTP request
that carried it.

Depends on lib.db.get_pool() for the shared asyncpg connection pool.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID

from lib import auth
from lib.db import get_pool


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

    Returns a dict containing the RAW token value (shown once) plus
    metadata. Callers must pass the raw `token` back to the user and
    never log it.
    """
    raw = auth.generate_token()
    token_hash = auth.hash_secret(raw)

    pool = await get_pool()
    row = await pool.fetchrow(
        """
        INSERT INTO auth_tokens (user_id, token_hash, label)
        VALUES ($1, $2, $3)
        RETURNING id, user_id, label, created_at
        """,
        user_id, token_hash, label,
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


async def resolve_token(raw_token: str) -> Optional[Dict[str, Any]]:
    """
    Look up an active (non-revoked) token by its raw string and return
    the owning user. Returns None if the token is invalid, revoked,
    or does not match any row.
    """
    if not auth.is_token_shaped(raw_token):
        return None

    pool = await get_pool()
    rows = await pool.fetch(
        """
        SELECT t.id, t.token_hash, t.user_id,
               u.username
        FROM auth_tokens t
        JOIN users u ON u.id = t.user_id
        WHERE t.revoked_at IS NULL
        """,
    )

    for row in rows:
        if auth.verify_secret(raw_token, row["token_hash"]):
            # Fire-and-forget last_used_at. If it races with another
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

    return None
