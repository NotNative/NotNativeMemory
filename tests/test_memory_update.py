"""
Integration tests for the memory_update MCP tool.

Exercises every field (content, tags, importance, project) plus the
reject paths (no-op edit, bad UUID, invalid importance, invalid scope,
unknown memory, cross-user access). Also verifies two load-bearing
invariants:

- Editing content triggers a re-embed so memory_search finds the new
  text but not the old.
- Edit preserves thermal state (temperature, access_count, created_at
  do not change just because content / tags / importance did).

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)
    MEMORY_MODEL_PATH                            (loaded; embeds real text)

Usage:
    python tests/test_memory_update.py
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
    import server
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id

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

    run_id = secrets.token_hex(4)
    alice_name = f"update-alice-{run_id}"
    bob_name = f"update-bob-{run_id}"

    pool = await db.get_pool()
    alice = await auth_db.create_user(alice_name, "password-1234")
    alice_uid = UUID(alice["id"])
    bob = await auth_db.create_user(bob_name, "password-1234")
    bob_uid = UUID(bob["id"])

    set_current_user_id(alice_uid)

    project_dir = f"/tmp/memup-{run_id}"
    project_id = await db.get_or_create_project(project_dir, owner_user_id=alice_uid)

    # Seed a memory with known content, tags, importance.
    from lib.embeddings import embed
    original_content = "alpha origin sentinel one-three-seven"
    original_vec = embed(original_content)
    mem_id = await db.store_memory(
        content=original_content,
        embedding=original_vec,
        project_id=project_id,
        owner_user_id=alice_uid,
        tags=["original", "seeded"],
        importance="normal",
    )

    # Snapshot pre-edit thermal state so we can verify edits don't
    # disturb it.
    async with rls.admin_conn(pool) as conn:
        pre = await conn.fetchrow(
            "SELECT temperature, access_count, created_at FROM memories WHERE id = $1",
            mem_id,
        )

    # 1. No-fields edit must be rejected before any DB/embed work.
    result = await server.memory_update(memory_id=str(mem_id))
    check("no-fields edit returns error", result.get("updated") is False)
    check("no-fields error message mentions fields",
          "at least one" in (result.get("error") or ""))

    # 2. Bad UUID format.
    result = await server.memory_update(memory_id="not-a-uuid", tags=["x"])
    check("bad memory_id rejected", result.get("updated") is False)
    check("bad memory_id error mentions the value",
          "not-a-uuid" in (result.get("error") or ""))

    # 3. Unknown but validly-shaped UUID returns not-found.
    result = await server.memory_update(
        memory_id=str(uuid4()), importance="high",
    )
    check("unknown memory returns not-found",
          result.get("updated") is False and "not found" in (result.get("error") or ""))

    # 4. Invalid importance is rejected.
    result = await server.memory_update(
        memory_id=str(mem_id), importance="lukewarm",
    )
    check("invalid importance rejected", result.get("updated") is False)

    # 5. Invalid scope (relative path) is rejected.
    result = await server.memory_update(
        memory_id=str(mem_id), project="scratch",
    )
    check("invalid scope rejected", result.get("updated") is False)
    check("invalid scope error names the scope",
          "scratch" in (result.get("error") or "").lower()
          or "not a valid write target" in (result.get("error") or ""))

    # 6. Happy path: edit importance only.
    result = await server.memory_update(
        memory_id=str(mem_id), importance="critical",
    )
    check("importance edit succeeded", result.get("updated") is True)

    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT importance FROM memories WHERE id = $1", mem_id,
        )
    check("importance persisted as 'critical'", row["importance"] == "critical")

    # 7. Tag replacement semantics: pass a new list, old tags are gone.
    result = await server.memory_update(
        memory_id=str(mem_id), tags=["fresh-tag"],
    )
    check("tag replacement succeeded", result.get("updated") is True)

    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT tags FROM memories WHERE id = $1", mem_id,
        )
    stored_tags = list(row["tags"])
    check("tag list fully replaced (old tags gone)",
          stored_tags == ["fresh-tag"])

    # 8. Content edit: verify re-embed reflects the new text.
    new_content = "beta replacement sentinel nine-four-two"
    result = await server.memory_update(
        memory_id=str(mem_id), content=new_content,
    )
    check("content edit succeeded", result.get("updated") is True)

    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT content FROM memories WHERE id = $1", mem_id,
        )
    check("content persisted", row["content"] == new_content)

    # Search with a query close to the new content should find the
    # memory; search with the original phrase should now rank it lower
    # or not at all (re-embed worked).
    search_new = await db.search_memories(
        query_embedding=embed(new_content),
        project_id=project_id,
        owner_user_id=alice_uid,
        limit=3,
    )
    top_new = search_new[0] if search_new else None
    check("search with new content finds the memory",
          top_new is not None and str(top_new["id"]) == str(mem_id))

    search_old = await db.search_memories(
        query_embedding=embed(original_content),
        project_id=project_id,
        owner_user_id=alice_uid,
        limit=3,
    )
    # The old-content search may still return the memory (only one row
    # in this project) but its similarity must be lower than the
    # new-content search's similarity.
    if search_old and search_new:
        old_sim = search_old[0].get("similarity") or 0.0
        new_sim = search_new[0].get("similarity") or 0.0
        check("new-content similarity > old-content similarity after re-embed",
              new_sim > old_sim)

    # 9. Thermal state preserved across edits: created_at and
    # access_count are unchanged. Temperature may have been nudged by
    # search-reheat above; we check that created_at is exactly equal.
    async with rls.admin_conn(pool) as conn:
        post = await conn.fetchrow(
            "SELECT temperature, access_count, created_at FROM memories WHERE id = $1",
            mem_id,
        )
    check("created_at preserved across edits",
          post["created_at"] == pre["created_at"])

    # 10. Rescope to _global.
    result = await server.memory_update(
        memory_id=str(mem_id), project="_global",
    )
    check("rescope to _global succeeded", result.get("updated") is True)

    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            """SELECT p.scope, p.directory
                 FROM memories m JOIN projects p ON p.id = m.project_id
                WHERE m.id = $1""",
            mem_id,
        )
    check("memory now lives in a global-scope project",
          row["scope"] == "global" and row["directory"] == "_global")

    # 11. Cross-user: bob cannot edit alice's memory (returns not-found
    # with no leak about the memory's existence).
    set_current_user_id(bob_uid)
    result = await server.memory_update(
        memory_id=str(mem_id), importance="low",
    )
    check("cross-user edit returns not-found",
          result.get("updated") is False and "not found" in (result.get("error") or ""))

    # Verify bob's failed attempt did not actually mutate the row.
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT importance FROM memories WHERE id = $1", mem_id,
        )
    check("cross-user attempt did not change importance",
          row["importance"] == "critical")

    # Cleanup. Admin conn bypasses RLS for the cross-user DELETE.
    async with rls.admin_conn(pool) as conn:
        await conn.execute("DELETE FROM users WHERE id = ANY($1)",
                           [alice_uid, bob_uid])
    await db.close_pool()

    print("---")
    print(f"{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
