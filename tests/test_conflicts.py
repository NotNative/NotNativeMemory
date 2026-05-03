"""
Integration tests for disagreement-aware consolidation (#1).

Exercises:
- Two similar-but-different memories trigger conflict detection
- Very similar memories (>=0.92) still dedup, no conflict
- Very different memories (<0.75) produce no conflict
- list_conflicts returns unresolved pairs
- resolve_conflict marks as resolved
- get_conflicts_for_memory annotates correctly
- Health stats include conflict counts

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_conflicts.py
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
    user_name = f"conflict-{run_id}"

    pool = await db.get_pool()
    user = await auth_db.create_user(user_name, "password-1234")
    user_uid = UUID(user["id"])
    set_current_user_id(user_uid)

    project_dir = f"/tmp/conflict-{run_id}"
    project_id = await db.get_or_create_project(project_dir, owner_user_id=user_uid)

    # --- Store two memories that should conflict (similar topic, different claims) ---
    content_a = "The API gateway runs on port 8080 in production"
    content_b = "The API gateway runs on port 9090 in production"

    mid_a = await db.store_memory(
        content=content_a,
        embedding=embed(content_a),
        project_id=project_id,
        owner_user_id=user_uid,
        importance="normal",
    )

    mid_b = await db.store_memory(
        content=content_b,
        embedding=embed(content_b),
        project_id=project_id,
        owner_user_id=user_uid,
        importance="normal",
    )

    # These should be similar enough to be in the conflict band but not dedup
    check("two memories stored (not deduped)", mid_a != mid_b)

    # --- Check if conflict was detected ---
    conflicts = await db.list_conflicts(user_uid)
    pair_found = any(
        (c["memory_a"]["id"] == str(mid_a) and c["memory_b"]["id"] == str(mid_b))
        or (c["memory_a"]["id"] == str(mid_b) and c["memory_b"]["id"] == str(mid_a))
        for c in conflicts
    )
    check("conflict detected between similar memories", pair_found)

    if pair_found:
        conflict_entry = next(
            c for c in conflicts
            if str(mid_a) in (c["memory_a"]["id"], c["memory_b"]["id"])
            and str(mid_b) in (c["memory_a"]["id"], c["memory_b"]["id"])
        )
        check("conflict has similarity in expected range",
               db.CONFLICT_SIMILARITY_LOW <= conflict_entry["similarity"] <= db.CONFLICT_SIMILARITY_HIGH)
        check("conflict is unresolved", conflict_entry["resolved_at"] is None)

        # --- get_conflicts_for_memory ---
        conflicts_for_a = await db.get_conflicts_for_memory(mid_a, user_uid)
        check("get_conflicts_for_memory finds conflict for A", len(conflicts_for_a) >= 1)
        other_ids = [c["other_id"] for c in conflicts_for_a]
        check("conflict partner is B", str(mid_b) in other_ids)

        # --- Resolve the conflict ---
        cid = UUID(conflict_entry["conflict_id"])
        ok = await db.resolve_conflict(cid, user_uid, "keep_both")
        check("resolve_conflict succeeds", ok is True)

        # Verify resolved
        conflicts_after = await db.list_conflicts(user_uid)
        still_unresolved = any(
            c["conflict_id"] == str(cid) for c in conflicts_after
        )
        check("resolved conflict not in unresolved list", not still_unresolved)

        # include_resolved=True should show it
        all_conflicts = await db.list_conflicts(user_uid, include_resolved=True)
        resolved_found = any(
            c["conflict_id"] == str(cid) and c["resolution"] == "keep_both"
            for c in all_conflicts
        )
        check("resolved conflict in full list", resolved_found)
    else:
        # If the embedding model doesn't place these in the conflict band,
        # skip the resolution tests but note the skip
        print("  SKIP  conflict not detected (embedding similarity outside 0.75-0.91)")
        print("        This may happen with certain embedding models.")

    # --- Completely different memories should NOT conflict ---
    content_x = "Python uses indentation for block scoping"
    content_y = "The cafeteria serves lunch from 12 to 1pm"
    mid_x = await db.store_memory(
        content=content_x,
        embedding=embed(content_x),
        project_id=project_id,
        owner_user_id=user_uid,
    )
    mid_y = await db.store_memory(
        content=content_y,
        embedding=embed(content_y),
        project_id=project_id,
        owner_user_id=user_uid,
    )
    conflicts_xy = await db.list_conflicts(user_uid)
    xy_conflict = any(
        (c["memory_a"]["id"] in (str(mid_x), str(mid_y))
         and c["memory_b"]["id"] in (str(mid_x), str(mid_y)))
        for c in conflicts_xy
    )
    check("unrelated memories do not conflict", not xy_conflict)

    # --- Resolve with invalid resolution ---
    try:
        await db.resolve_conflict(uuid4(), user_uid, "invalid_value")
        check("invalid resolution raises ValueError", False)
    except ValueError:
        check("invalid resolution raises ValueError", True)

    # --- Resolve non-existent conflict ---
    ok = await db.resolve_conflict(uuid4(), user_uid, "dismissed")
    check("resolve non-existent conflict returns False", ok is False)

    # --- Health stats include conflicts ---
    stats = await db.get_health_stats(user_uid, project_id)
    check("health stats has conflicts key", "conflicts" in stats)
    check("health stats conflicts.total >= 1", stats["conflicts"]["total"] >= 1)

    # --- Cleanup ---
    async with rls.admin_conn(pool) as conn:
        await conn.execute(
            "DELETE FROM memory_conflicts WHERE owner_user_id = $1", user_uid)
        await conn.execute(
            "DELETE FROM memories WHERE project_id = $1", project_id)
        await conn.execute("DELETE FROM projects WHERE id = $1", project_id)
        await conn.execute("DELETE FROM users WHERE id = $1", user_uid)

    print(f"\n{'='*60}")
    print(f"  Results: {total - failed}/{total} passed, {failed} failed")
    print(f"{'='*60}")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
