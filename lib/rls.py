"""
Row-Level Security helper.

Ships as scaffolding for a future full activation. The policies
themselves live in config/migrations/008_rls_foundations.sql but are
inert until:

    1. A non-superuser DB role is created for application connections.
       Postgres superusers always bypass RLS, so RLS does literally
       nothing when the app connects as a superuser (the default for
       the stock pgvector Docker image).

    2. RLS is ENABLED on the user-scoped tables (see the commented
       ALTER TABLE block at the bottom of 008_rls_foundations.sql).

    3. db.py call sites that touch user-scoped tables use `app_conn`
       below (or set app.current_user manually inside a transaction).

Until all three pieces are in place, the Phase 7 per-user owner_user_id
filters in lib/db.py remain the only enforcement. That is where they
have always been, and RLS layered on top is defense-in-depth — a
safety net for a future forgotten WHERE clause, not a current hole.

Usage (post-activation):

    from lib.rls import app_conn

    async with app_conn(pool, current_user_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM memories WHERE id = $1", memory_id,
        )

`app_conn` acquires a connection from the pool, sets the session-
level GUC `app.current_user`, yields the connection to the caller,
and RESETs the GUC on release. The RESET means the next acquirer of
this connection starts clean — this is the "small race window"
design: between RESET and the next SET, the connection's context is
unset (which under our RLS policy means zero rows visible, not a
different user's). A pathological race would only surface as "saw
nothing," never as "saw the wrong user's data."

Error paths are covered by the async-with contract: if the block
raises, RESET still runs on exit via the try/finally inside
contextlib.asynccontextmanager.
"""

from __future__ import annotations

import contextlib
from typing import AsyncIterator
from uuid import UUID


@contextlib.asynccontextmanager
async def app_conn(pool, user_id: UUID) -> AsyncIterator:
    """
    Acquire a pooled connection with `app.current_user` set to the
    given user id for RLS enforcement. RESETs on exit so stale
    context cannot leak to the next pool acquirer.

    Safe to use whether RLS is enabled or disabled on the underlying
    tables. When disabled, the SET / RESET are no-ops from the RLS
    perspective; the helper costs one extra round trip per acquire
    but is otherwise invisible.
    """
    async with pool.acquire() as conn:
        # Quote the uid explicitly via parameter binding. set_config's
        # signature is (name, value, is_local) — is_local=false means
        # session-level, which outlasts the implicit transaction of
        # a single statement; we clean it up in the finally below.
        await conn.execute(
            "SELECT set_config('app.current_user', $1::text, false)",
            str(user_id),
        )
        try:
            yield conn
        finally:
            # Reset so the connection returns to the pool without
            # carrying this request's identity. A subsequent acquirer
            # that forgets to call app_conn sees zero rows under an
            # active RLS policy (fail-closed), not another user's data.
            try:
                await conn.execute("SELECT set_config('app.current_user', '', false)")
            except Exception:
                # Connection is being torn down; the reset does not
                # matter because the connection won't be reused.
                pass
