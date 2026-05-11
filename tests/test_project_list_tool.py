"""
Integration tests for memory_project_list MCP tool and the underlying
db.list_user_projects helper.

Covers:
- Empty case: new user with no projects returns count=0.
- Multiple scopes (local, _domain_*, _global) all surface with the
  right scope value.
- scope filter narrows the result set.
- include_counts adds the memory_count field with correct values.
- Owner isolation: another user's projects are NOT visible.
- Invalid scope arg returns error without crashing.
- Auth-required gating.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_project_list_tool.py
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
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id

    import server
    project_list = server.memory_project_list.fn

    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    run_id = secrets.token_hex(4)
    user_a = await auth_db.create_user(f"proj-list-a-{run_id}", "password-1234")
    user_b = await auth_db.create_user(f"proj-list-b-{run_id}", "password-1234")
    uid_a = UUID(user_a["id"])
    uid_b = UUID(user_b["id"])
    pool = await db.get_pool()

    try:
        # --- Empty case (user A has no projects yet) ---
        set_current_user_id(uid_a)
        out = await project_list()
        check("empty user: count=0", out.get("count") == 0)
        check("empty user: projects is empty list", out.get("projects") == [])

        # --- Create three projects across scopes for user A ---
        local_id = await db.get_or_create_project(
            f"/tmp/proj-list-local-{run_id}", owner_user_id=uid_a,
        )
        domain_id = await db.get_or_create_project(
            f"_domain_test_{run_id}", owner_user_id=uid_a,
        )
        global_id = await db.get_or_create_project(
            "_global", owner_user_id=uid_a,
        )

        out = await project_list()
        check("after creates: count=3", out.get("count") == 3)
        scopes = sorted(p["scope"] for p in out["projects"])
        check("after creates: scopes are domain/global/local",
              scopes == ["domain", "global", "local"])

        # Each row has the contract fields.
        contract_keys = {"id", "directory", "name", "scope", "domains", "created_at"}
        check("every row has contract keys",
              all(set(p.keys()) >= contract_keys for p in out["projects"]))

        # --- Scope filter ---
        out_local = await project_list(scope="local")
        check("scope=local: only local rows",
              out_local["count"] == 1 and out_local["projects"][0]["scope"] == "local")

        out_domain = await project_list(scope="domain")
        check("scope=domain: only domain rows",
              out_domain["count"] == 1 and out_domain["projects"][0]["scope"] == "domain")

        out_global = await project_list(scope="global")
        check("scope=global: only global rows",
              out_global["count"] == 1 and out_global["projects"][0]["scope"] == "global")

        # Invalid scope
        out = await project_list(scope="weirdo")
        check("invalid scope: error returned",
              "error" in out and out.get("count") == 0)

        # --- include_counts ---
        # Seed two memories into the local project so the count is meaningful.
        from lib.embeddings import EMBEDDING_DIM

        def vec(axis: int):
            v = [0.0] * EMBEDDING_DIM
            v[axis] = 1.0
            return v

        await db.store_memory(
            content=f"local-mem-1-{run_id}",
            embedding=vec(0),
            project_id=local_id,
            owner_user_id=uid_a,
        )
        await db.store_memory(
            content=f"local-mem-2-{run_id}",
            embedding=vec(1),
            project_id=local_id,
            owner_user_id=uid_a,
        )

        out = await project_list(include_counts=True)
        by_scope = {p["scope"]: p for p in out["projects"]}
        check("include_counts: local memory_count=2",
              by_scope["local"]["memory_count"] == 2)
        check("include_counts: domain memory_count=0",
              by_scope["domain"]["memory_count"] == 0)
        check("include_counts: global memory_count=0",
              by_scope["global"]["memory_count"] == 0)

        # Without include_counts, no memory_count key present.
        out_nocount = await project_list()
        check("default: no memory_count field",
              all("memory_count" not in p for p in out_nocount["projects"]))

        # --- Owner isolation: switch to user B ---
        set_current_user_id(uid_b)
        out_b = await project_list()
        check("user B sees zero of user A's projects",
              out_b["count"] == 0)

        # User B creates one project; still does not see A's.
        await db.get_or_create_project(
            f"/tmp/proj-list-b-{run_id}", owner_user_id=uid_b,
        )
        out_b = await project_list()
        check("user B sees only own project",
              out_b["count"] == 1
              and out_b["projects"][0]["scope"] == "local")

        # --- Auth gating ---
        set_current_user_id(None)
        out = await project_list()
        check("no auth -> refused",
              "error" in out and "authentication" in out["error"].lower())

    finally:
        set_current_user_id(uid_a)
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                "DELETE FROM memories WHERE owner_user_id = ANY($1)",
                [uid_a, uid_b],
            )
            await conn.execute(
                "DELETE FROM projects WHERE owner_user_id = ANY($1)",
                [uid_a, uid_b],
            )
        await pool.execute(
            "DELETE FROM users WHERE id = ANY($1)", [uid_a, uid_b],
        )
        await db.close_pool()

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
