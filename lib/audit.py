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
from typing import Any, Mapping, Optional
from uuid import UUID


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
