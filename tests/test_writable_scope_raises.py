"""
Regression test for the silent-validator-rejection footgun.

Before the fix, writing to a non-writable scope (e.g. the "general"
sentinel) returned `{"stored": false, "error": "..."}` inside
content[0].text while the MCP envelope still carried isError=false.
Clients that only inspected the envelope (memory_store_call did)
counted the rejection as a success.

Fix: server.py now RAISES `ToolError` on scope-validation failure.
FastMCP's lowlevel handler catches the exception and produces a
`CallToolResult(isError=True)`, so envelope-only checkers see the
failure too.

These tests pin the raise contract directly at the tool function. The
full envelope shape (isError=true) is delegated to FastMCP's standard
exception path covered by upstream tests.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_writable_scope_raises.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


async def run() -> int:
    from mcp.server.fastmcp.exceptions import ToolError
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id
    import server

    memory_store = server.memory_store.fn
    memory_fact_add = server.memory_fact_add.fn
    memory_update = server.memory_update.fn

    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    run_id = secrets.token_hex(4)
    user = await auth_db.create_user(f"scope-raise-{run_id}", "password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)
    pool = await db.get_pool()

    try:
        # Each test forces an invalid scope and asserts ToolError is raised.
        # "general" is the original footgun — the sentinel the server used
        # to fall through to when project was omitted. Validator must reject
        # it loudly now.
        for scope in ("general", "bare-name", "relative/path", ""):
            try:
                await memory_store(content="x" * 20, project=scope)
                check(f"memory_store(project={scope!r}) raised ToolError", False)
            except ToolError as exc:
                msg = str(exc).lower()
                check(
                    f"memory_store(project={scope!r}) raised ToolError",
                    "valid write target" in msg or "rejected" in msg
                    or scope.lower() in msg or "scope" in msg,
                )
            except Exception as exc:  # noqa: BLE001
                check(
                    f"memory_store(project={scope!r}) raised ToolError "
                    f"(got {type(exc).__name__})",
                    False,
                )

        # memory_fact_add uses the same helper; cover one bad scope.
        try:
            await memory_fact_add(
                subject="x", predicate="y", object="z", project="general",
            )
            check("memory_fact_add raised ToolError on 'general'", False)
        except ToolError:
            check("memory_fact_add raised ToolError on 'general'", True)

        # memory_update has its own validation path (not via the shared
        # helper). Seed a real memory, then attempt to rescope it to
        # 'general'.
        legit_project = await db.get_or_create_project(
            f"/tmp/scope-raise-{run_id}", owner_user_id=uid,
        )
        from lib.embeddings import EMBEDDING_DIM

        def vec(axis: int):
            v = [0.0] * EMBEDDING_DIM
            v[axis] = 1.0
            return v

        mid = await db.store_memory(
            content=f"baseline-{run_id}", embedding=vec(0),
            project_id=legit_project, owner_user_id=uid,
        )
        try:
            await memory_update(memory_id=str(mid), project="general")
            check("memory_update raised ToolError on 'general' rescope", False)
        except ToolError:
            check("memory_update raised ToolError on 'general' rescope", True)

        # Valid scope is unchanged: it should succeed without raising.
        out = await memory_store(content=f"valid-{run_id}", project="_global")
        check("memory_store(project='_global') succeeds without raise",
              out.get("stored") is True)

        # Harmless source aliases are repaired at the MCP boundary so
        # smaller local models do not fail on "user" vs. "user-stated".
        out = await memory_store(
            content=f"source-alias-{run_id}",
            project="_global",
            source="user",
        )
        warnings = out.get("warnings") or []
        check("memory_store(source='user') succeeds",
              out.get("stored") is True)
        check("memory_store(source='user') reports normalization warning",
              any(w.get("code") == "source_normalized" for w in warnings))

    finally:
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                "DELETE FROM memories WHERE owner_user_id = $1", uid,
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
