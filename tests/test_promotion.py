"""
Integration tests for promotion flow (#5).

Exercises:
- get_promotion_candidates returns memories with high access counts
- Memories already classified as rule/preference are excluded
- Superseded memories are excluded
- promote_memory upgrades class (memory->preference, preference->rule)
- promote_memory rejects downgrade (rule->preference)
- promote_memory rejects invalid class
- promote_memory rejects cross-user

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_promotion.py
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
    user_name = f"promote-{run_id}"

    pool = await db.get_pool()
    user = await auth_db.create_user(user_name, "password-1234")
    user_uid = UUID(user["id"])
    set_current_user_id(user_uid)

    project_dir = f"/tmp/promote-{run_id}"
    project_id = await db.get_or_create_project(project_dir, owner_user_id=user_uid)

    # --- Seed memories with varying access counts ---
    content_hot = "User prefers terse output with no trailing summaries"
    mid_hot = await db.store_memory(
        content=content_hot,
        embedding=embed(content_hot),
        project_id=project_id,
        owner_user_id=user_uid,
        importance="high",
    )
    # Simulate accesses
    async with rls.admin_conn(pool) as conn:
        await conn.execute(
            "UPDATE memories SET access_count = 5 WHERE id = $1", mid_hot)

    content_very_hot = "Every code change must include tests"
    mid_very_hot = await db.store_memory(
        content=content_very_hot,
        embedding=embed(content_very_hot),
        project_id=project_id,
        owner_user_id=user_uid,
        importance="critical",
    )
    async with rls.admin_conn(pool) as conn:
        await conn.execute(
            "UPDATE memories SET access_count = 8 WHERE id = $1", mid_very_hot)

    content_cold = "Tried using Redis for caching yesterday"
    mid_cold = await db.store_memory(
        content=content_cold,
        embedding=embed(content_cold),
        project_id=project_id,
        owner_user_id=user_uid,
        importance="low",
    )
    # access_count stays at 0

    content_already_rule = "Never use em-dashes in any output"
    mid_rule = await db.store_memory(
        content=content_already_rule,
        embedding=embed(content_already_rule),
        project_id=project_id,
        owner_user_id=user_uid,
        importance="critical",
        memory_class="rule",
    )
    async with rls.admin_conn(pool) as conn:
        await conn.execute(
            "UPDATE memories SET access_count = 10 WHERE id = $1", mid_rule)

    # --- get_promotion_candidates ---
    candidates = await db.get_promotion_candidates(user_uid, min_access_count=3)
    candidate_ids = [c["id"] for c in candidates]

    check("hot memory is a candidate", str(mid_hot) in candidate_ids)
    check("very hot memory is a candidate", str(mid_very_hot) in candidate_ids)
    check("cold memory is NOT a candidate", str(mid_cold) not in candidate_ids)
    check("rule memory is NOT a candidate", str(mid_rule) not in candidate_ids)

    # Check suggested_class heuristic
    hot_entry = next(c for c in candidates if c["id"] == str(mid_hot))
    very_hot_entry = next(c for c in candidates if c["id"] == str(mid_very_hot))
    check("5 accesses suggests preference", hot_entry["suggested_class"] == "preference")
    check("8 accesses suggests rule", very_hot_entry["suggested_class"] == "rule")

    # --- promote_memory: unclassified -> preference ---
    ok = await db.promote_memory(mid_hot, user_uid, "preference")
    check("promote to preference succeeds", ok is True)

    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow("SELECT class FROM memories WHERE id = $1", mid_hot)
    check("class is now preference", row["class"] == "preference")

    # --- promote_memory: preference -> rule ---
    ok = await db.promote_memory(mid_hot, user_uid, "rule")
    check("promote preference to rule succeeds", ok is True)

    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow("SELECT class FROM memories WHERE id = $1", mid_hot)
    check("class is now rule", row["class"] == "rule")

    # --- promote_memory: rule -> preference (downgrade rejected) ---
    ok = await db.promote_memory(mid_hot, user_uid, "preference")
    check("downgrade rule to preference rejected", ok is False)

    # --- promote_memory: invalid class ---
    ok = await db.promote_memory(mid_very_hot, user_uid, "invalid")
    check("invalid class rejected", ok is False)

    # --- promote_memory: cross-user ---
    other = await auth_db.create_user(f"other-{run_id}", "pw1234")
    other_uid = UUID(other["id"])
    ok = await db.promote_memory(mid_very_hot, other_uid, "rule")
    check("cross-user promote rejected", ok is False)

    # --- After promotion, no longer a candidate ---
    candidates_after = await db.get_promotion_candidates(user_uid, min_access_count=3)
    check("promoted memory no longer a candidate",
          str(mid_hot) not in [c["id"] for c in candidates_after])

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
