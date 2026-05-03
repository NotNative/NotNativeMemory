"""
Integration tests for user-driven supersede (#6).

Exercises:
- Supersede marks old memory, old stops appearing in search
- Supersede auto-resolves conflicts between the pair
- Cannot supersede a memory you don't own
- Cannot supersede an already-superseded memory
- Cannot supersede a memory with itself
- Superseded memory still visible in direct lookup (audit)

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_supersede.py
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
    from lib.embeddings import embed

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
    user_name = f"supersede-{run_id}"

    pool = await db.get_pool()
    user = await auth_db.create_user(user_name, "password-1234")
    user_uid = UUID(user["id"])
    set_current_user_id(user_uid)

    project_dir = f"/tmp/supersede-{run_id}"
    project_id = await db.get_or_create_project(project_dir, owner_user_id=user_uid)

    # --- Create old and new memories ---
    content_old = "Deploy target is atlas server at 192.168.1.50 for all containers"
    content_new = "Deploy target migrated to jill server. Atlas is decommissioned."

    mid_old = await db.store_memory(
        content=content_old,
        embedding=embed(content_old),
        project_id=project_id,
        owner_user_id=user_uid,
        importance="normal",
    )

    mid_new = await db.store_memory(
        content=content_new,
        embedding=embed(content_new),
        project_id=project_id,
        owner_user_id=user_uid,
        importance="normal",
    )
    check("two memories stored", mid_old != mid_new)

    # --- Verify old appears in search before supersede ---
    results = await db.search_memories(
        query_embedding=embed("deploy target server"),
        project_id=project_id,
        owner_user_id=user_uid,
        limit=10,
    )
    old_in_search = any(r["id"] == str(mid_old) for r in results)
    check("old memory in search before supersede", old_in_search)

    # --- Supersede ---
    ok = await db.supersede_memory(mid_old, mid_new, user_uid)
    check("supersede_memory returns True", ok is True)

    # --- Old no longer in search ---
    results = await db.search_memories(
        query_embedding=embed("deploy target server"),
        project_id=project_id,
        owner_user_id=user_uid,
        limit=10,
    )
    old_in_search_after = any(r["id"] == str(mid_old) for r in results)
    check("old memory NOT in search after supersede", not old_in_search_after)

    # New should still appear
    new_in_search = any(r["id"] == str(mid_new) for r in results)
    check("new memory still in search", new_in_search)

    # --- Old still exists in DB (audit) ---
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT superseded_by FROM memories WHERE id = $1", mid_old)
    check("old memory has superseded_by set", row["superseded_by"] == mid_new)

    # --- Cannot supersede again ---
    ok = await db.supersede_memory(mid_old, mid_new, user_uid)
    check("cannot supersede already-superseded memory", ok is False)

    # --- Cannot supersede self ---
    ok = await db.supersede_memory(mid_new, mid_new, user_uid)
    check("cannot supersede self", ok is False)

    # --- Cross-user rejected ---
    other = await auth_db.create_user(f"other-{run_id}", "pw1234")
    other_uid = UUID(other["id"])
    mid_other = await db.store_memory(
        content="unrelated memory for other user test",
        embedding=embed("unrelated memory for other user test"),
        project_id=project_id,
        owner_user_id=user_uid,
        importance="low",
    )
    ok = await db.supersede_memory(mid_other, mid_new, other_uid)
    check("cross-user supersede rejected", ok is False)

    # --- Auto-resolve conflict test ---
    content_c1 = "Redis runs on the default port 6379 on atlas"
    content_c2 = "Redis was moved to port 6380 on jill after migration"
    mid_c1 = await db.store_memory(
        content=content_c1,
        embedding=embed(content_c1),
        project_id=project_id,
        owner_user_id=user_uid,
    )
    mid_c2 = await db.store_memory(
        content=content_c2,
        embedding=embed(content_c2),
        project_id=project_id,
        owner_user_id=user_uid,
    )

    # Check if a conflict exists between them
    conflicts_before = await db.list_conflicts(user_uid)
    has_conflict = any(
        (c["memory_a"]["id"] in (str(mid_c1), str(mid_c2))
         and c["memory_b"]["id"] in (str(mid_c1), str(mid_c2)))
        for c in conflicts_before
    )

    if has_conflict:
        # Supersede should auto-resolve
        ok = await db.supersede_memory(mid_c1, mid_c2, user_uid)
        check("supersede with conflict succeeds", ok is True)
        conflicts_after = await db.list_conflicts(user_uid)
        conflict_resolved = not any(
            (c["memory_a"]["id"] in (str(mid_c1), str(mid_c2))
             and c["memory_b"]["id"] in (str(mid_c1), str(mid_c2)))
            for c in conflicts_after
        )
        check("conflict auto-resolved after supersede", conflict_resolved)
    else:
        # Embedding model didn't flag them as conflicting -- just supersede
        ok = await db.supersede_memory(mid_c1, mid_c2, user_uid)
        check("supersede without prior conflict succeeds", ok is True)
        print("  SKIP  auto-resolve test (no conflict detected by embeddings)")

    # --- Unknown ID ---
    ok = await db.supersede_memory(uuid4(), mid_new, user_uid)
    check("supersede unknown old_id returns False", ok is False)

    # --- Cleanup ---
    async with rls.admin_conn(pool) as conn:
        await conn.execute(
            "DELETE FROM memory_conflicts WHERE owner_user_id = $1", user_uid)
        await conn.execute(
            "DELETE FROM memories WHERE project_id = $1", project_id)
        await conn.execute("DELETE FROM projects WHERE id = $1", project_id)
        await conn.execute("DELETE FROM users WHERE id = $1", user_uid)
        await conn.execute("DELETE FROM users WHERE id = $1", other_uid)

    print(f"\n{'='*60}")
    print(f"  Results: {total - failed}/{total} passed, {failed} failed")
    print(f"{'='*60}")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
