"""
Integration tests for memory_project_delete MCP tool.

Covers:
- Deleting a populated project cascades memories AND facts AND RAG
  documents away; counts in the response reflect what was reaped.
- Deleting an empty project succeeds and returns zero child counts.
- Invalid UUID returns an error without touching the DB.
- Unknown project id returns projects_deleted=0 without raising.
- Owner isolation: user B cannot delete user A's project even with
  the right id.
- Auth gating: no current user -> refused.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_project_delete_tool.py
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
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id
    from lib.embeddings import EMBEDDING_DIM
    import server

    project_delete = server.memory_project_delete.fn

    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    def vec(axis: int):
        v = [0.0] * EMBEDDING_DIM
        v[axis] = 1.0
        return v

    run_id = secrets.token_hex(4)
    user_a = await auth_db.create_user(f"pd-a-{run_id}", "password-1234")
    user_b = await auth_db.create_user(f"pd-b-{run_id}", "password-1234")
    uid_a = UUID(user_a["id"])
    uid_b = UUID(user_b["id"])
    pool = await db.get_pool()

    try:
        set_current_user_id(uid_a)

        # ---- Populated project: memories + facts cascade ----
        proj_full = await db.get_or_create_project(
            f"/tmp/pd-full-{run_id}", owner_user_id=uid_a,
        )
        await db.store_memory(
            content=f"m1-{run_id}", embedding=vec(0),
            project_id=proj_full, owner_user_id=uid_a,
        )
        await db.store_memory(
            content=f"m2-{run_id}", embedding=vec(1),
            project_id=proj_full, owner_user_id=uid_a,
        )
        await db.add_fact(
            project_id=proj_full, subject="s", predicate="p", obj="o",
            owner_user_id=uid_a,
        )

        out = await project_delete(str(proj_full))
        check("populated delete: projects_deleted=1",
              out.get("projects_deleted") == 1)
        check("populated delete: 2 memories cascaded",
              out.get("memories") == 2)
        check("populated delete: 1 fact cascaded",
              out.get("facts") == 1)

        # Verify the rows are actually gone.
        async with rls.admin_conn(pool) as conn:
            row = await conn.fetchrow(
                "SELECT count(*) AS n FROM projects WHERE id = $1",
                proj_full,
            )
            check("project row gone from DB", int(row["n"]) == 0)

            row = await conn.fetchrow(
                "SELECT count(*) AS n FROM memories WHERE project_id = $1",
                proj_full,
            )
            check("memory rows gone from DB", int(row["n"]) == 0)

        # ---- Empty project: succeeds, zero child counts ----
        proj_empty = await db.get_or_create_project(
            f"/tmp/pd-empty-{run_id}", owner_user_id=uid_a,
        )
        out = await project_delete(str(proj_empty))
        check("empty delete: projects_deleted=1",
              out.get("projects_deleted") == 1)
        check("empty delete: memories=0",
              out.get("memories") == 0)
        check("empty delete: facts=0",
              out.get("facts") == 0)

        # ---- Invalid UUID ----
        out = await project_delete("not-a-uuid")
        check("invalid uuid: error returned, no delete",
              out.get("projects_deleted") == 0
              and "Invalid project_id" in out.get("error", ""))

        # ---- Unknown id ----
        out = await project_delete(str(uuid4()))
        check("unknown id: projects_deleted=0 with explanation",
              out.get("projects_deleted") == 0
              and "not found" in out.get("error", "").lower())

        # ---- Owner isolation ----
        proj_a_owned = await db.get_or_create_project(
            f"/tmp/pd-cross-{run_id}", owner_user_id=uid_a,
        )
        await db.store_memory(
            content=f"a-only-{run_id}", embedding=vec(2),
            project_id=proj_a_owned, owner_user_id=uid_a,
        )

        # Switch to user B and try to delete A's project.
        set_current_user_id(uid_b)
        out = await project_delete(str(proj_a_owned))
        check("user B cannot delete user A's project",
              out.get("projects_deleted") == 0)

        # A's data must still exist.
        set_current_user_id(uid_a)
        async with rls.app_conn(pool, uid_a) as conn:
            row = await conn.fetchrow(
                "SELECT count(*) AS n FROM projects WHERE id = $1",
                proj_a_owned,
            )
            check("user A's project survived B's delete attempt",
                  int(row["n"]) == 1)

        # ---- Auth gating ----
        set_current_user_id(None)
        out = await project_delete(str(uuid4()))
        check("no auth: refused",
              "authentication" in out.get("error", "").lower())

    finally:
        set_current_user_id(uid_a)
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                "DELETE FROM memories WHERE owner_user_id = ANY($1)",
                [uid_a, uid_b],
            )
            await conn.execute(
                "DELETE FROM facts WHERE owner_user_id = ANY($1)",
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
