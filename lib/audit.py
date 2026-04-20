"""
Audit log writer.

Append-only trail of security-relevant events: logins (success and
failure), logouts, token mints and revokes, registrations. The table
is defined in config/migrations/009_audit_events.sql; this module is
the only place that INSERTs into it.

Design choices:

    - Best-effort writes. An audit INSERT that fails (DB blip, schema
      drift during a migration) should never block the user-facing
      operation. The log_event coroutine catches Exception and emits
      to the application logger instead, so forensic gaps are
      observable but the login/register path keeps working.

    - No PII in detail_json. We store IP and user-agent (both things
      the user already sent us on the wire), event type, and a
      target id. Passwords, raw tokens, session cookies — never.
      The per-field cap on detail is implicit via Postgres jsonb
      limits; callers should keep detail small (a handful of keys).

    - Event types are dotted strings, not an enum. New events can
      be added by convention; operators filter via WHERE event_type
      IN (...). A future formalization to a CHECK constraint is a
      migration away.

Canonical event types:

    login.success      A user successfully exchanged credentials.
    login.fail         Bad credentials for an existing user, OR
                       the username didn't exist. Actor is NULL.
    login.rate_limited Rate-limit bucket denied the request.
    logout             User logged out (cookie cleared; token
                       revoked best-effort).
    token.mint         A Bearer token was created.
    token.revoke       A Bearer token was revoked.
    user.register      A new user was created via open registration.
    register.rate_limited Rate-limit bucket denied registration.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional
from uuid import UUID


# -- Helpers for route handlers -------------------------------------------

_SINCE_PRESETS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def parse_since_preset(raw: Optional[str]) -> Optional[datetime]:
    """Map a preset string ("1h", "24h", "7d", "30d") to a concrete
    UTC cutoff datetime. Unknown / empty inputs return None (no filter)."""
    if not raw:
        return None
    delta = _SINCE_PRESETS.get(raw)
    if delta is None:
        return None
    return datetime.now(timezone.utc) - delta


_log = logging.getLogger("notnative.audit")


async def log_event(
    event_type: str,
    *,
    actor_user_id: Optional[UUID] = None,
    target_id: Optional[UUID] = None,
    detail: Optional[Mapping[str, Any]] = None,
) -> None:
    """
    Best-effort insert into audit_events. Never raises. Caller must
    not rely on the event being written — this is forensic, not
    transactional.

    Detail is serialized as JSON. Keep it small and free of secrets.
    """
    from lib.db import get_pool

    try:
        pool = await get_pool()
        await pool.execute(
            """
            INSERT INTO audit_events (actor_user_id, event_type, target_id, detail)
            VALUES ($1, $2, $3, $4::jsonb)
            """,
            actor_user_id,
            event_type,
            target_id,
            json.dumps(dict(detail or {})),
        )
    except Exception as exc:
        # Forensic gap is visible in application logs; the caller's
        # primary operation is not aborted.
        _log.debug(
            "audit: failed to write event %r (%s)", event_type, exc,
        )


def request_detail(request) -> dict:
    """
    Extract the handful of request-shape fields we actually want in
    every event's detail payload. IP, user-agent, path. Safe to
    re-call from any route handler.
    """
    from lib.rate_limit import client_ip

    return {
        "ip": client_ip(request),
        "ua": request.headers.get("user-agent", "")[:256],
        "path": getattr(request.url, "path", "") if hasattr(request, "url") else "",
    }


async def list_events(
    *,
    offset: int = 0,
    limit: int = 50,
    actor_username: Optional[str] = None,
    event_type: Optional[str] = None,
    since=None,
):
    """
    Return (events, total_count) for the admin audit-log view.

    Filter values are optional; any mix produces a WHERE clause with
    parameter binding. All three filters only ever compare strings or
    timestamps — no identifier interpolation, so dynamic SQL here is
    safe against injection.

    `events` is a list of dicts with:
        id, actor_user_id, actor_username (LEFT JOIN; None when the
        user is deleted or actor is NULL), event_type, target_id,
        detail (dict, parsed from jsonb), at (ISO string).
    """
    import json
    from lib.db import get_pool

    pool = await get_pool()

    where: list[str] = []
    args: list = []
    if actor_username:
        where.append(f"u.username = ${len(args) + 1}")
        args.append(actor_username)
    if event_type:
        where.append(f"a.event_type = ${len(args) + 1}")
        args.append(event_type)
    if since is not None:
        where.append(f"a.at >= ${len(args) + 1}")
        args.append(since)
    where_sql = " AND ".join(where) if where else "true"

    total_row = await pool.fetchrow(
        f"""
        SELECT COUNT(*)::bigint AS n
        FROM audit_events a
        LEFT JOIN users u ON u.id = a.actor_user_id
        WHERE {where_sql}
        """,
        *args,
    )
    total = int(total_row["n"] or 0)

    rows = await pool.fetch(
        f"""
        SELECT a.id, a.actor_user_id, u.username AS actor_username,
               a.event_type, a.target_id,
               a.detail::text AS detail_json, a.at
        FROM audit_events a
        LEFT JOIN users u ON u.id = a.actor_user_id
        WHERE {where_sql}
        ORDER BY a.at DESC
        LIMIT ${len(args) + 1} OFFSET ${len(args) + 2}
        """,
        *args, limit, offset,
    )

    events = []
    for r in rows:
        try:
            detail = json.loads(r["detail_json"] or "{}")
        except (ValueError, TypeError):
            detail = {}
        events.append({
            "id": str(r["id"]),
            "actor_user_id": str(r["actor_user_id"]) if r["actor_user_id"] else None,
            "actor_username": r["actor_username"],
            "event_type": r["event_type"],
            "target_id": str(r["target_id"]) if r["target_id"] else None,
            "detail": detail,
            "at": r["at"].isoformat(),
        })

    return events, total


async def list_event_types():
    """Return the distinct set of event_type values currently in the table,
    alphabetically. Used to populate the event-type filter dropdown."""
    from lib.db import get_pool
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT DISTINCT event_type FROM audit_events ORDER BY event_type"
    )
    return [r["event_type"] for r in rows]
