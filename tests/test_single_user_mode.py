"""
Regression test for the implicit single-user mode and the transition
into multi-user via claim_admin_and_transfer_data.

Single-user mode is the default for fresh installs: zero admins
exist, the auth middleware authenticates every request as the owner
sentinel, no Bearer token required. The operator opts into multi-user
by visiting /enable-multiuser and claiming the first admin; on claim,
the sentinel's data transfers to the new admin and the middleware
flips to Bearer-required.

This test exercises:
- ensure_owner_sentinel idempotently creates and returns the owner
- HTTP requests in single-user mode authenticate as owner with no token
- A memory stored as owner carries owner_user_id = sentinel.id
- claim_admin_and_transfer_data with a bad token raises ValueError
- claim_admin_and_transfer_data with a valid token + creds creates
  the admin, transfers all owner-owned data, deletes the sentinel,
  and removes the bootstrap file
- After invalidate_admin_cache, the middleware now requires a Bearer
  token (single-user bypass no longer fires)

Heavyweight integration: hits a live pgvector and renders an HTTP
request through the real middleware stack via httpx.ASGITransport.
Uses admin_conn for setup/teardown so the test is not filtered by
its own RLS context.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_single_user_mode.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


async def run() -> int:
    import server
    from lib import (
        admin_bootstrap,
        auth_db,
        auth_middleware,
        db,
        rls,
    )
    from lib.auth_middleware import (
        BearerAuthMiddleware,
        invalidate_admin_cache,
    )
    from lib.limits import BodySizeLimitMiddleware
    from lib.security_headers import SecurityHeadersMiddleware

    failed = 0
    total = 0

    def check(label, cond):
        nonlocal failed, total
        total += 1
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    pool = await db.get_pool()

    # -- Test setup: clean slate ---------------------------------------
    # Wipe any existing admins and any leftover sentinel from prior
    # runs. Other tests use random-suffix usernames so we leave them
    # alone. The bootstrap file is also cleared so the lazy write
    # produces a known state.
    async with rls.admin_conn(pool) as conn:
        await conn.execute("DELETE FROM users WHERE is_admin = true")
        await conn.execute(
            "DELETE FROM users WHERE username = $1",
            auth_db.OWNER_SENTINEL_USERNAME,
        )
    admin_bootstrap.delete_bootstrap_file()
    invalidate_admin_cache()

    # -- 1. Single-user mode is the default ----------------------------
    initial_admins = await auth_db.count_admins()
    check("count_admins == 0 with no admins (single-user mode)",
          initial_admins == 0)

    # -- 2. Owner sentinel is created on first need --------------------
    owner = await auth_db.ensure_owner_sentinel()
    owner_uid = UUID(owner["id"])
    check("ensure_owner_sentinel returns the owner row",
          owner["username"] == auth_db.OWNER_SENTINEL_USERNAME)
    check("owner sentinel is_admin is False",
          owner["is_admin"] is False)

    # Idempotent: calling again returns the same row.
    owner_again = await auth_db.ensure_owner_sentinel()
    check("ensure_owner_sentinel is idempotent",
          owner_again["id"] == owner["id"])

    # -- 3. HTTP requests authenticate as owner without a token --------
    # Replicate the middleware stack the server installs.
    app = server.mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware)
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
    ) as client:
        # /memories is not whitelisted; under multi-user mode it would
        # return 401 to an anonymous caller. Under single-user mode the
        # middleware should fold the caller into the owner identity and
        # the page should render or redirect, not 401.
        resp = await client.get("/memories", follow_redirects=False)
        check("single-user mode: GET /memories without token is not 401",
              resp.status_code != 401)

    # -- 4. Owner-owned data lives under sentinel.id ------------------
    test_project_dir = f"/tmp/single-user-{secrets.token_hex(4)}"
    project_id = await db.get_or_create_project(
        test_project_dir, owner_user_id=owner_uid,
    )
    sentinel_memory_id = await db.store_memory(
        content="memory authored in single-user mode",
        embedding=[0.0] * 1024,
        project_id=project_id,
        owner_user_id=owner_uid,
        importance="normal",
    )
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT owner_user_id FROM memories WHERE id = $1",
            sentinel_memory_id,
        )
    check("owner-authored memory carries sentinel owner_user_id",
          row is not None and row["owner_user_id"] == owner_uid)

    # -- 5. claim_admin_and_transfer_data validates the token ---------
    raised = False
    try:
        await auth_db.claim_admin_and_transfer_data(
            "wrong-token", "would-be-admin", "password-1234",
        )
    except ValueError as exc:
        raised = "invalid bootstrap token" in str(exc)
    check("claim_admin_and_transfer_data rejects an invalid token",
          raised)

    # -- 6. With a valid token, the transition succeeds ---------------
    # ensure_bootstrap_if_needed writes (or preserves) the file.
    bootstrap_path = await admin_bootstrap.ensure_bootstrap_if_needed()
    check("bootstrap path returned when no admin exists",
          bootstrap_path is not None and os.path.isfile(bootstrap_path))

    bootstrap_token = admin_bootstrap.read_bootstrap_token()
    check("bootstrap token readable from disk",
          isinstance(bootstrap_token, str) and len(bootstrap_token) > 0)

    admin_username = f"admin-{secrets.token_hex(4)}"
    result = await auth_db.claim_admin_and_transfer_data(
        bootstrap_token, admin_username, "claim-test-password-1234",
    )
    admin = result["admin"]
    transferred = result["transferred"]
    admin_uid = UUID(admin["id"])

    check("admin was created with the requested username",
          admin["username"] == admin_username)
    check("admin is_admin = True",
          admin["is_admin"] is True)
    check("transferred report includes memories table",
          "memories" in transferred and transferred["memories"] >= 1)

    # -- 7. count_admins now == 1 (multi-user mode) -------------------
    check("count_admins == 1 after claim",
          (await auth_db.count_admins()) == 1)

    # -- 8. Owner sentinel is gone ------------------------------------
    check("owner sentinel deleted",
          (await auth_db.get_user_by_username(
              auth_db.OWNER_SENTINEL_USERNAME)) is None)

    # -- 9. Bootstrap file is gone ------------------------------------
    check("bootstrap file removed after claim",
          not admin_bootstrap.bootstrap_file_exists())

    # -- 10. The owner-authored memory now belongs to admin -----------
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT owner_user_id FROM memories WHERE id = $1",
            sentinel_memory_id,
        )
    check("transferred memory now carries admin owner_user_id",
          row is not None and row["owner_user_id"] == admin_uid)

    # -- 11. Multi-user mode is enforced after cache invalidation -----
    invalidate_admin_cache()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver",
    ) as client:
        resp = await client.get("/memories", follow_redirects=False)
        check("multi-user mode: GET /memories without token is now 401",
              resp.status_code == 401)

    # -- Cleanup ------------------------------------------------------
    # Drop the admin we just created and any data they own. CASCADE
    # via FK on users handles the dependent rows.
    async with rls.admin_conn(pool) as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", admin_uid)
    invalidate_admin_cache()
    await db.close_pool()

    print("---")
    print(f"{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
