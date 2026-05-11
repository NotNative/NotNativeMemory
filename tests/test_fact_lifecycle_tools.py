"""
Integration tests for memory_fact_update and memory_fact_forget MCP tools.

Both wrap existing lib/db helpers (`update_fact` / `forget_fact`)
which are already covered by `test_fact_update.py` and the temporal
tests. This file covers the MCP tool surface specifically: arg
validation, auth gating, and the return-shape contract callers depend
on.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_fact_lifecycle_tools.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID, uuid4

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


async def run() -> int:
    from lib import auth_db, db
    from lib.auth_context import set_current_user_id

    # Import the MCP tools as plain functions for in-process call.
    # FastMCP's @mcp.tool decorator preserves the wrapped function;
    # accessing .fn gives us the underlying coroutine.
    import server
    fact_update = server.memory_fact_update.fn
    fact_forget = server.memory_fact_forget.fn

    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    run_id = secrets.token_hex(4)
    test_username = f"fact-tools-test-{run_id}"

    pool = await db.get_pool()
    user = await auth_db.create_user(test_username, "password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)

    try:
        # Set up: create a project and a fact.
        project_id = await db.get_or_create_project(
            f"/tmp/fact-tools-{run_id}", owner_user_id=uid,
        )
        added = await db.add_fact(
            project_id=project_id,
            subject="hostA",
            predicate="port",
            obj="5432",
            owner_user_id=uid,
        )
        fid = added["id"]

        # ------- memory_fact_update -------

        # Invalid UUID
        out = await fact_update("not-a-uuid", object="9999")
        check("update: invalid uuid -> error",
              not out.get("updated") and "Invalid fact_id" in out.get("error", ""))

        # No fields -> error
        out = await fact_update(fid)
        check("update: no fields -> error",
              not out.get("updated") and "No fields" in out.get("error", ""))

        # Valid update
        out = await fact_update(fid, object="5433")
        check("update: object change -> updated=True",
              out.get("updated") is True and out.get("id") == fid)

        # Verify persisted
        facts = await db.query_facts(
            owner_user_id=uid, subject="hostA", project_id=project_id,
        )
        check("update: change persisted in DB",
              any(f["object"] == "5433" for f in facts))

        # Update of unknown id -> updated=False with error
        out = await fact_update(str(uuid4()), object="x")
        check("update: unknown id -> updated=False",
              not out.get("updated") and "not found" in out.get("error", "").lower())

        # ------- memory_fact_forget -------

        # Invalid UUID
        out = await fact_forget("not-a-uuid")
        check("forget: invalid uuid -> error",
              not out.get("forgotten") and "Invalid fact_id" in out.get("error", ""))

        # Unknown id
        out = await fact_forget(str(uuid4()))
        check("forget: unknown id -> forgotten=False",
              not out.get("forgotten"))

        # Real delete
        out = await fact_forget(fid)
        check("forget: real id -> forgotten=True",
              out.get("forgotten") is True)

        # Verify gone
        facts = await db.query_facts(
            owner_user_id=uid, subject="hostA", project_id=project_id,
        )
        check("forget: row deleted from DB",
              all(str(f.get("id", "")) != fid for f in facts))

        # ------- Auth gating -------
        # Clear the user context and confirm both tools refuse.
        set_current_user_id(None)
        out = await fact_update(str(uuid4()), object="x")
        check("update: no auth -> refused",
              not out.get("updated") and "authentication" in out.get("error", "").lower())
        out = await fact_forget(str(uuid4()))
        check("forget: no auth -> refused",
              not out.get("forgotten") and "authentication" in out.get("error", "").lower())

    finally:
        set_current_user_id(uid)
        from lib import rls
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                "DELETE FROM facts WHERE owner_user_id = $1", uid,
            )
            await conn.execute(
                "DELETE FROM projects WHERE owner_user_id = $1", uid,
            )
        await pool.execute("DELETE FROM users WHERE id = $1", uid)
        await db.close_pool()

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
