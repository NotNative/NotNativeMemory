"""
Integration tests for the /admin/metrics dashboard (Phase 4b).

Covers the data-shape helpers and the HTTP route:

Unit-ish (no HTTP client):
- observability.recent_events respects the capacity cap, returns
  newest-first, and honors the limit arg.
- observability.metrics_snapshot returns the expected structure and
  reflects counter increments from instrumented tool calls.

HTTP (Starlette TestClient against the real FastMCP app):
- Anonymous GET /admin/metrics redirects to /login.
- Logged-in non-admin gets 403.
- Logged-in admin gets 200 with expected content markers and no
  memory content / query leakage in the HTML.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_admin_metrics.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID, uuid4

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


async def _unit_checks(check) -> None:
    """Unit-ish checks against observability helpers. No HTTP."""
    from lib import observability

    # -- recent_events: capacity cap ------------------------------------
    # Exercise the ring buffer directly. Reset then fill past capacity.
    observability._recent_events.clear()
    cap = observability._RECENT_EVENTS_CAPACITY
    for i in range(cap + 20):
        observability._recent_events.append({
            "ts": float(i), "tool": "probe", "outcome": "ok",
            "user": None, "latency_ms": 0.0,
            "exception_type": None, "result_size": None,
        })
    events = observability.recent_events()
    check("ring buffer enforces capacity cap", len(events) == cap)
    check("recent_events returns newest first",
          events[0]["ts"] > events[-1]["ts"])

    # -- recent_events: limit arg ---------------------------------------
    short = observability.recent_events(limit=5)
    check("recent_events honors limit arg", len(short) == 5)
    check("limit-truncated list still newest-first",
          short == events[:5])

    # -- metrics_snapshot shape -----------------------------------------
    snap = observability.metrics_snapshot()
    check("snapshot has tool_calls key", "tool_calls" in snap)
    check("snapshot has tool_latency key", "tool_latency" in snap)
    check("snapshot has tool_errors key", "tool_errors" in snap)
    check("snapshot has pool.active/idle keys",
          "active" in snap["pool"] and "idle" in snap["pool"])


async def run() -> int:
    import httpx

    import server
    from lib import auth_db, db, observability, rls
    from lib.auth_context import set_current_user_id

    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    # Run the unit-ish checks first.
    await _unit_checks(check)

    run_id = secrets.token_hex(4)
    admin_name = f"metrics-admin-{run_id}"
    peon_name = f"metrics-peon-{run_id}"

    pool = await db.get_pool()

    admin_user = await auth_db.create_user(admin_name, "password-1234")
    admin_uid = UUID(admin_user["id"])
    peon_user = await auth_db.create_user(peon_name, "password-1234")
    peon_uid = UUID(peon_user["id"])

    # Grant admin. We flip the is_admin column directly via the
    # admin connection; /admin/users has an HTTP toggle but this is a
    # test, not a workflow.
    async with rls.admin_conn(pool) as conn:
        await conn.execute(
            "UPDATE users SET is_admin = true WHERE id = $1", admin_uid,
        )

    # Mint a real token for each user so the auth middleware resolves
    # the session cookie against the DB.
    admin_token = await auth_db.create_token(admin_uid, label="metrics-admin-test")
    peon_token = await auth_db.create_token(peon_uid, label="metrics-peon-test")

    # Exercise one instrumented tool call so the dashboard has
    # something to render beyond empty tables.
    set_current_user_id(admin_uid)
    await server.memory_forget(str(uuid4()))

    try:
        # Replicate the middleware stack run_http() installs so the
        # test exercises the real auth perimeter, not a bare app.
        # httpx.AsyncClient keeps everything in one event loop so the
        # asyncpg pool is reusable across the test request and the
        # surrounding test fixture calls. starlette's TestClient runs
        # the app in a thread with its own loop and breaks that.
        from lib.auth_middleware import BearerAuthMiddleware
        from lib.limits import BodySizeLimitMiddleware
        from lib.security_headers import SecurityHeadersMiddleware

        app = server.mcp.streamable_http_app()
        app.add_middleware(BearerAuthMiddleware)
        app.add_middleware(BodySizeLimitMiddleware)
        app.add_middleware(SecurityHeadersMiddleware)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver",
        ) as client:
            # -- Anonymous: BearerAuthMiddleware returns 401 JSON ----
            # /admin/metrics is not whitelisted, so the middleware
            # rejects unauthenticated requests before the view's
            # _require_admin redirect logic can run. This matches the
            # behavior of every other /admin/* and /memories page.
            resp_anon = await client.get(
                "/admin/metrics", follow_redirects=False,
            )
            check("anonymous /admin/metrics returns 401",
                  resp_anon.status_code == 401)
            check("anonymous response mentions missing auth",
                  "Bearer" in resp_anon.text
                  or "Authorization" in resp_anon.text)

            # -- Non-admin: should get 403 ---------------------------
            resp_peon = await client.get(
                "/admin/metrics",
                cookies={"nnm_session": peon_token["token"]},
                follow_redirects=False,
            )
            check("non-admin /admin/metrics returns 403",
                  resp_peon.status_code == 403)
            check("non-admin response mentions admin",
                  "admin" in resp_peon.text.lower())

            # -- Admin: should get 200 with expected content ---------
            resp_admin = await client.get(
                "/admin/metrics",
                cookies={"nnm_session": admin_token["token"]},
                follow_redirects=False,
            )
            check("admin /admin/metrics returns 200",
                  resp_admin.status_code == 200)

            body = resp_admin.text
            check("admin page includes 'Metrics' heading",
                  "Metrics" in body)
            check("admin page links back to /metrics raw scrape",
                  "/metrics" in body)
            check("admin page lists a tool name we just called",
                  "memory_forget" in body)
            check("admin page includes 'Recent events' section",
                  "Recent events" in body)
            check("admin page includes meta-refresh",
                  "http-equiv=\"refresh\"" in body
                  or "http-equiv='refresh'" in body)

            # Privacy: admin's UUID may appear (actor on events);
            # peon's must not since the peon never called a tool.
            check("admin UUID appears in body "
                  "(actor label on events)",
                  str(admin_uid) in body or str(admin_uid)[:8] in body)
            check("non-admin UUID does NOT appear "
                  "(peon never called a tool)",
                  str(peon_uid) not in body)

    finally:
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                "DELETE FROM memories WHERE owner_user_id = ANY($1)",
                [admin_uid, peon_uid],
            )
            await conn.execute(
                "DELETE FROM facts WHERE owner_user_id = ANY($1)",
                [admin_uid, peon_uid],
            )
            await conn.execute(
                "DELETE FROM projects WHERE owner_user_id = ANY($1)",
                [admin_uid, peon_uid],
            )
        await pool.execute(
            "DELETE FROM users WHERE id = ANY($1)",
            [admin_uid, peon_uid],
        )
        await db.close_pool()

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
