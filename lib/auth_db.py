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


async def count_admins() -> int:
    """
    Return number of users with is_admin=true. Used by the auth
    middleware to decide single-user vs multi-user mode: zero admins
    means the server is still in its single-user default and every
    caller authenticates as the owner sentinel; one or more admins
    means the operator has explicitly opted into multi-user and a
    Bearer token is required.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT COUNT(*) AS n FROM users WHERE is_admin = true",
    )
    return int(row["n"] or 0)


# Root principal username used once an instance is claimed. The root
# principal is represented as an admin user internally so the existing
# auth, RLS, token, and admin-route machinery stays compatible.
ROOT_PRINCIPAL_USERNAME = "root"


# Sentinel username used in single-user mode. Reserved: callers cannot
# register this name through the web flow (the auth_routes.register
# handler rejects it). Auto-created on first need by
# ensure_owner_sentinel and removed by the claim flows when the
# operator claims the instance.
OWNER_SENTINEL_USERNAME = "owner"


async def ensure_owner_sentinel() -> Dict[str, Any]:
    """
    Return the owner-sentinel user, creating it if missing.

    Single-user mode authenticates every request as this user. The
    password is a random throwaway since the only way to log in as
    owner is the auth-middleware bypass that fires when no admins
    exist; the password field exists only so create_user's normal
    constraints are satisfied.

    Idempotent. The username is uniqueness-constrained so concurrent
    creators race cleanly: one inserts, the other gets the existing
    row on retry.
    """
    import secrets as _secrets
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT id, username, is_admin, created_at FROM users WHERE username = $1",
        OWNER_SENTINEL_USERNAME,
    )
    if row is not None:
        return _row_to_user(row)

    # First-create path. Fall back to a fresh SELECT if a concurrent
    # creator beat us (UniqueViolation).
    import asyncpg as _asyncpg
    throwaway = _secrets.token_urlsafe(48)
    hashed = auth.hash_secret(throwaway)
    try:
        row = await pool.fetchrow(
            """
            INSERT INTO users (username, password_hash)
            VALUES ($1, $2)
            RETURNING id, username, is_admin, created_at
            """,
            OWNER_SENTINEL_USERNAME, hashed,
        )
    except _asyncpg.UniqueViolationError:
        row = await pool.fetchrow(
            "SELECT id, username, is_admin, created_at FROM users WHERE username = $1",
            OWNER_SENTINEL_USERNAME,
        )
    return _row_to_user(row)


# Tables that carry owner_user_id and need to follow the user during a
# single-user -> multi-user data transfer. auth_tokens is owned-but-
# different (the column is user_id, not owner_user_id) and is handled
# separately. audit_events deliberately does NOT transfer; historical
# actions stay attributed to whoever performed them.
_OWNER_SCOPED_TABLES = (
    "memories",
    "facts",
    "projects",
    "documents",
    "doc_chunks",
    "ingestion_jobs",
)


async def transfer_owned_data(from_uid: UUID, to_uid: UUID) -> Dict[str, int]:
    """
    Reassign ownership of every owner_scoped row from one user to
    another. Returns a per-table count of rows touched. Intended for
    the single-user -> claimed-root transition: when root is claimed,
    the data the owner sentinel accumulated transfers to root so the
    legacy instance keeps its existing memories.

    Runs under admin_conn so the UPDATEs are not filtered by RLS
    (which would otherwise see zero rows for the admin's session).
    """
    from lib import rls
    pool = await get_pool()
    counts: Dict[str, int] = {}
    async with rls.admin_conn(pool) as conn:
        for table in _OWNER_SCOPED_TABLES:
            try:
                result = await conn.execute(
                    f"UPDATE {table} SET owner_user_id = $2 WHERE owner_user_id = $1",
                    from_uid, to_uid,
                )
            except Exception:
                # A table may not exist yet on a partial install; skip
                # quietly and let the rest proceed.
                continue
            try:
                counts[table] = int(result.split()[-1])
            except (ValueError, IndexError):
                counts[table] = 0
    return counts


async def claim_admin_and_transfer_data(
    bootstrap_token: str,
    username: str,
    password: str,
) -> Dict[str, Any]:
    """
    Single-shot transition from single-user mode to multi-user.

    Validates the bootstrap token against the on-disk file, creates a
    fresh admin user, transfers the owner sentinel's data to that
    admin, deletes the sentinel, and removes the bootstrap file.

    Returns the new admin's user dict plus a summary of how much data
    moved. Raises ValueError on any validation failure (bad token,
    bad credentials, no sentinel to transfer from). Raises
    asyncpg.UniqueViolationError if the requested admin username is
    already in use.

    Caller is responsible for invalidating any cached "is multi-user"
    flag in the auth middleware after this returns.
    """
    from lib import admin_bootstrap

    if not admin_bootstrap.validate_bootstrap_token(bootstrap_token):
        raise ValueError("invalid bootstrap token")

    sentinel = await get_user_by_username(OWNER_SENTINEL_USERNAME)
    if sentinel is None:
        # No sentinel means we are not in single-user mode. The web
        # handler should have caught this and redirected to /login.
        raise ValueError("no owner sentinel exists; not in single-user mode")

    # Create the admin first. If username clash, the
    # UniqueViolationError surfaces before any data moves.
    admin = await create_user(username, password)
    admin_uid = UUID(admin["id"])
    await set_admin(admin_uid, True)
    admin["is_admin"] = True

    # Move every owner_user_id row over.
    sentinel_uid = sentinel["id"]
    counts = await transfer_owned_data(sentinel_uid, admin_uid)

    # Delete the sentinel last so the FK cascades have nothing to
    # cascade (data already moved). audit_events references actor_user_id
    # ON DELETE SET NULL, so historical sentinel actions become null-
    # actor entries (acceptable; the sentinel is anonymous by design).
    await delete_user(sentinel_uid)

    # Delete the bootstrap file. Token should not survive a successful
    # claim or a future restart could resurrect it for re-use.
    admin_bootstrap.delete_bootstrap_file()

    return {"admin": admin, "transferred": counts}


async def claim_root_and_transfer_data(
    bootstrap_token: str,
    token_label: str = "root-claim",
) -> Dict[str, Any]:
    """
    Claim the NNM instance root with a bootstrap token.

    This is the root-token version of the legacy first-admin flow. It
    creates or reuses the reserved ``root`` principal, marks it admin,
    transfers any single-user owner-sentinel data to root, deletes the
    sentinel, removes the bootstrap file, and returns a freshly minted
    Bearer token. The raw token is shown exactly once by the caller.
    """
    from lib import admin_bootstrap

    if not admin_bootstrap.validate_bootstrap_token(bootstrap_token):
        raise ValueError("invalid bootstrap token")

    sentinel = await get_user_by_username(OWNER_SENTINEL_USERNAME)
    root = await ensure_user(ROOT_PRINCIPAL_USERNAME)
    root_uid = UUID(root["id"])
    await set_admin(root_uid, True)
    root["is_admin"] = True

    counts: Dict[str, int] = {}
    if sentinel is not None and UUID(str(sentinel["id"])) != root_uid:
        counts = await transfer_owned_data(UUID(str(sentinel["id"])), root_uid)
        await delete_user(UUID(str(sentinel["id"])))

    admin_bootstrap.delete_bootstrap_file()
    token = await create_token(root_uid, label=token_label or "root-claim")

    return {"root": root, "admin": root, "transferred": counts, "token": token}


async def set_admin(user_id: UUID, is_admin: bool) -> None:
    """
    Promote or demote a user's admin flag. The claim-admin flow calls
    this with is_admin=True; reset-admin clears it with is_admin=False
    (for all existing admins). No route accepts is_admin as a payload;
    this function is the only write surface.
    """
    pool = await get_pool()
    await pool.execute(
        "UPDATE users SET is_admin = $2 WHERE id = $1",
        user_id, is_admin,
    )


async def list_admin_ids() -> List[UUID]:
    """Return the user_ids of every current admin. Used by reset-admin
    to iterate token_generation bumps after demoting them."""
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT id FROM users WHERE is_admin = true",
    )
    return [row["id"] for row in rows]


async def list_users_overview(
    offset: int = 0, limit: int = 50, search: Optional[str] = None,
) -> tuple:
    """
    Admin-only listing of users with scalar-subquery counts for the
    admin dashboard. Returns (users, total). Each user dict carries:

        id, username, is_admin, created_at,
        memory_count, fact_count, project_count, active_token_count

    `search` does a case-insensitive prefix match on username when
    provided; None or empty returns everyone.

    Counts are scalar subqueries rather than a big JOIN + GROUP BY
    because the result set is small (operators have dozens of users,
    not millions) and the plan is equivalent in that regime.

    Runs under admin_conn — the scalar subqueries cross user
    boundaries (counting every user's rows, not just the admin's),
    so RLS policies under FORCE RLS would return zero for every user
    other than the admin without the sentinel bypass.
    """
    from lib import rls
    pool = await get_pool()

    where = "true"
    args: list = []
    if search:
        where = "u.username ILIKE $1"
        args.append(search + "%")

    async with rls.admin_conn(pool) as conn:
        total_row = await conn.fetchrow(
            f"SELECT COUNT(*)::bigint AS n FROM users u WHERE {where}",
            *args,
        )
        total = int(total_row["n"] or 0)

        limit_idx = len(args) + 1
        offset_idx = len(args) + 2
        rows = await conn.fetch(
            f"""
            SELECT
                u.id, u.username, u.is_admin, u.created_at,
                (SELECT COUNT(*) FROM memories    m WHERE m.owner_user_id = u.id) AS memory_count,
                (SELECT COUNT(*) FROM facts       f WHERE f.owner_user_id = u.id) AS fact_count,
                (SELECT COUNT(*) FROM projects    p WHERE p.owner_user_id = u.id) AS project_count,
                (SELECT COUNT(*) FROM auth_tokens t WHERE t.user_id = u.id AND t.revoked_at IS NULL) AS active_token_count
            FROM users u
            WHERE {where}
            ORDER BY u.created_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *args, limit, offset,
        )

    users = [
        {
            "id": str(r["id"]),
            "username": r["username"],
            "is_admin": bool(r["is_admin"]),
            "created_at": r["created_at"].isoformat(),
            "memory_count": int(r["memory_count"] or 0),
            "fact_count": int(r["fact_count"] or 0),
            "project_count": int(r["project_count"] or 0),
            "active_token_count": int(r["active_token_count"] or 0),
        }
        for r in rows
    ]
    return users, total


async def delete_user(user_id: UUID) -> bool:
    """
    Hard-delete a user. Cascades via FK to memories, facts, projects,
    auth_tokens. Audit_events carries ON DELETE SET NULL on actor_user_id
    so the historical trail survives with NULL actor. Returns True when
    a row was removed; False when the UUID does not match any user.
    """
    pool = await get_pool()
    result = await pool.execute("DELETE FROM users WHERE id = $1", user_id)
    return result.endswith(" 1")


async def set_password(user_id: UUID, new_password: str) -> None:
    """
    Overwrite a user's password with a fresh scrypt hash. Per-field
    length caps apply (same as create_user). Caller should pair this
    with bump_token_generation so the old sessions die — the session-
    revocation is NOT automatic because not every caller wants it
    (a self-service password change might want to keep the current
    session valid; an admin-triggered reset always wants to kill the
    other sessions).
    """
    if not new_password or len(new_password) < 8:
        raise ValueError("password must be at least 8 characters")
    try:
        enforce_field_len(new_password, MAX_PASSWORD_BYTES, "password")
    except PayloadTooLarge as exc:
        raise ValueError(str(exc))

    hashed = auth.hash_secret(new_password)
    pool = await get_pool()
    result = await pool.execute(
        "UPDATE users SET password_hash = $2 WHERE id = $1",
        user_id, hashed,
    )
    if not result.endswith(" 1"):
        raise ValueError(f"no such user: {user_id}")


async def create_user(username: str, password: str) -> Dict[str, Any]:
    """
    Create a user. Raises asyncpg.UniqueViolationError if the username
    is taken. Hashes the password with scrypt before writing.

    Create a human/service principal. Admin/root status is assigned by
    explicit bootstrap or maintenance flows, never from this payload.
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
        RETURNING id, username, is_admin, created_at
        """,
        username.strip(), hashed,
    )
    return _row_to_user(row)


async def ensure_user(username: str) -> Dict[str, Any]:
    """
    Idempotently create or return a user for service-to-service
    provisioning flows.

    NNO uses this to create hidden NNM principals for its own users.
    The generated password is intentionally random and not returned;
    the caller receives a Bearer token minted separately via
    create_token().
    """
    import asyncpg as _asyncpg
    import secrets as _secrets

    existing = await get_user_by_username(username)
    if existing is not None:
        return _row_to_user(existing)

    password = _secrets.token_urlsafe(48)
    try:
        return await create_user(username, password)
    except _asyncpg.UniqueViolationError:
        existing = await get_user_by_username(username)
        if existing is None:
            raise
        return _row_to_user(existing)


async def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """
    Lookup a user by username. Returns dict INCLUDING password_hash
    for login verification, or None if no such user.
    """
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        SELECT id, username, password_hash, is_admin, created_at
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
        SELECT id, username, is_admin, created_at
        FROM users WHERE id = $1
        """,
        user_id,
    )
    return _row_to_user(row) if row else None


def _row_to_user(row) -> Dict[str, Any]:
    return {
        "id": str(row["id"]),
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
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

    from lib import rls
    pool = await get_pool()
    async with rls.app_conn(pool, user_id) as conn:
        row = await conn.fetchrow(
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
    from lib import rls
    pool = await get_pool()
    async with rls.app_conn(pool, user_id) as conn:
        rows = await conn.fetch(
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
    from lib import rls
    pool = await get_pool()
    async with rls.app_conn(pool, user_id) as conn:
        result = await conn.execute(
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

    from lib import rls
    pool = await get_pool()
    # Pre-auth: we don't know the user yet. Runs as admin so RLS on
    # auth_tokens doesn't filter the lookup row out before we can
    # verify the secret. Once verified, downstream code re-enters
    # with the authenticated user's context via app_conn.
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            """
            SELECT t.id, t.token_hash, t.user_id,
                   u.username, u.is_admin
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
        await conn.execute(
            "UPDATE auth_tokens SET last_used_at = now() WHERE id = $1",
            row["id"],
        )
    return {
        "token_id": str(row["id"]),
        "user_id": row["user_id"],
        "username": row["username"],
        "is_admin": bool(row["is_admin"]),
    }
