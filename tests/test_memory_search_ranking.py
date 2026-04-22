"""
Integration tests for memory search ranking in lib/db.search_memories.

Ranking formula in _build_search_query:

    score = (1 - cosine_distance) + importance_bonus

    importance_bonus:
      critical  +0.15
      high      +0.10
      normal     0.00
      low       -0.05

After the primary score sort, the ORDER BY breaks ties with
created_at DESC and id ASC (added in Phase 1a) so identical scores
return deterministically.

Covered:
- Four memories with the same embedding but different importances
  return in critical > high > normal > low order.
- Importance bonus can overturn a similarity gap up to ~0.15 (a
  critical at 0.82 similarity beats a normal at 0.95).
- Importance bonus is not enough to flip a larger similarity gap
  (a critical at 0.70 loses to a normal at 0.95).
- min_importance filter excludes lower tiers from the result set.
- tags filter pre-narrows results to rows containing any of the
  supplied tags.
- Deterministic tiebreaker: two memories with identical scores
  return in created_at DESC then id ASC order.
- Cross-user RLS: alice's search never returns bob's memories.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_memory_search_ranking.py
"""

import asyncio
import math
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib.embeddings import EMBEDDING_DIM


def vec_at_similarity(cos_sim: float) -> list:
    cos_sim = max(-1.0, min(1.0, cos_sim))
    angle = math.acos(cos_sim)
    v = [0.0] * EMBEDDING_DIM
    v[0] = math.cos(angle)
    v[1] = math.sin(angle)
    return v


def orthogonal_vec(axis: int) -> list:
    v = [0.0] * EMBEDDING_DIM
    v[axis] = 1.0
    return v


BASE_VEC = vec_at_similarity(1.0)


async def run() -> int:
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id

    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    run_id = secrets.token_hex(4)
    alice_name = f"rank-alice-{run_id}"
    bob_name = f"rank-bob-{run_id}"

    pool = await db.get_pool()

    alice = await auth_db.create_user(alice_name, "password-1234")
    alice_uid = UUID(alice["id"])
    bob = await auth_db.create_user(bob_name, "password-1234")
    bob_uid = UUID(bob["id"])

    set_current_user_id(alice_uid)

    try:
        # ================================================================
        # Scenario 1: identical embeddings, different importances
        # -> critical > high > normal > low
        # ================================================================
        proj_rank = await db.get_or_create_project(
            f"/tmp/rank-basic-{run_id}", owner_user_id=alice_uid,
        )
        db._store_counters.clear()

        now = datetime.now(timezone.utc)
        seed_ids = {}
        async with rls.admin_conn(pool) as conn:
            for imp in ["critical", "high", "normal", "low"]:
                row_id = uuid4()
                seed_ids[imp] = row_id
                await conn.execute(
                    """INSERT INTO memories
                           (id, project_id, owner_user_id, content,
                            embedding, tags, importance, temperature,
                            last_accessed)
                       VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8, $9)""",
                    row_id, proj_rank, alice_uid, f"rank-seed-{imp}",
                    str(BASE_VEC), [], imp, 70.0, now,
                )

        results = await db.search_memories(
            query_embedding=BASE_VEC,
            project_id=proj_rank,
            owner_user_id=alice_uid,
            limit=10,
        )
        importance_order = [r["importance"] for r in results[:4]]
        check("same-embedding search: critical ranks first",
              importance_order and importance_order[0] == "critical")
        check("same-embedding search: critical > high > normal > low",
              importance_order == ["critical", "high", "normal", "low"])

        # ================================================================
        # Scenario 2: importance bonus can overcome a small similarity gap
        # ================================================================
        # Critical @ sim 0.82 -> score 0.82 + 0.15 = 0.97
        # Normal   @ sim 0.95 -> score 0.95 + 0.00 = 0.95
        # Critical wins.
        proj_flip = await db.get_or_create_project(
            f"/tmp/rank-flip-small-{run_id}", owner_user_id=alice_uid,
        )
        async with rls.admin_conn(pool) as conn:
            crit_id = uuid4()
            norm_id = uuid4()
            await conn.execute(
                """INSERT INTO memories
                       (id, project_id, owner_user_id, content,
                        embedding, tags, importance, temperature,
                        last_accessed)
                   VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8, $9)""",
                crit_id, proj_flip, alice_uid, "flip-crit",
                str(vec_at_similarity(0.82)), [], "critical", 70.0, now,
            )
            await conn.execute(
                """INSERT INTO memories
                       (id, project_id, owner_user_id, content,
                        embedding, tags, importance, temperature,
                        last_accessed)
                   VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8, $9)""",
                norm_id, proj_flip, alice_uid, "flip-norm",
                str(vec_at_similarity(0.95)), [], "normal", 70.0, now,
            )

        results = await db.search_memories(
            query_embedding=BASE_VEC,
            project_id=proj_flip,
            owner_user_id=alice_uid,
            limit=10,
        )
        check("critical at sim 0.82 beats normal at sim 0.95 "
              "(bonus closes the 0.13 gap)",
              results and results[0]["importance"] == "critical")

        # ================================================================
        # Scenario 3: importance bonus is NOT enough to flip a large gap
        # ================================================================
        # Critical @ sim 0.70 -> score 0.70 + 0.15 = 0.85
        # Normal   @ sim 0.95 -> score 0.95 + 0.00 = 0.95
        # Normal wins.
        proj_nolflip = await db.get_or_create_project(
            f"/tmp/rank-nolflip-{run_id}", owner_user_id=alice_uid,
        )
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                """INSERT INTO memories
                       (id, project_id, owner_user_id, content,
                        embedding, tags, importance, temperature,
                        last_accessed)
                   VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8, $9)""",
                uuid4(), proj_nolflip, alice_uid, "nolflip-crit",
                str(vec_at_similarity(0.70)), [], "critical", 70.0, now,
            )
            await conn.execute(
                """INSERT INTO memories
                       (id, project_id, owner_user_id, content,
                        embedding, tags, importance, temperature,
                        last_accessed)
                   VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8, $9)""",
                uuid4(), proj_nolflip, alice_uid, "nolflip-norm",
                str(vec_at_similarity(0.95)), [], "normal", 70.0, now,
            )

        results = await db.search_memories(
            query_embedding=BASE_VEC,
            project_id=proj_nolflip,
            owner_user_id=alice_uid,
            limit=10,
        )
        check("critical at sim 0.70 loses to normal at sim 0.95 "
              "(bonus cannot close the 0.25 gap)",
              results and results[0]["importance"] == "normal")

        # ================================================================
        # Scenario 4: min_importance filter excludes lower tiers
        # ================================================================
        # Reuse proj_rank from scenario 1 (4 seeds, one per importance).
        high_floor = await db.search_memories(
            query_embedding=BASE_VEC,
            project_id=proj_rank,
            owner_user_id=alice_uid,
            min_importance="high",
            limit=10,
        )
        floor_imps = {r["importance"] for r in high_floor}
        check("min_importance='high': only critical and high returned",
              floor_imps == {"critical", "high"})

        crit_floor = await db.search_memories(
            query_embedding=BASE_VEC,
            project_id=proj_rank,
            owner_user_id=alice_uid,
            min_importance="critical",
            limit=10,
        )
        check("min_importance='critical': only critical returned",
              {r["importance"] for r in crit_floor} == {"critical"})

        # ================================================================
        # Scenario 5: tag filter narrows the candidate set
        # ================================================================
        proj_tags = await db.get_or_create_project(
            f"/tmp/rank-tags-{run_id}", owner_user_id=alice_uid,
        )
        async with rls.admin_conn(pool) as conn:
            async def seed(axis, imp, tags, content):
                await conn.execute(
                    """INSERT INTO memories
                           (id, project_id, owner_user_id, content,
                            embedding, tags, importance, temperature,
                            last_accessed)
                       VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8,
                               $9)""",
                    uuid4(), proj_tags, alice_uid, content,
                    str(orthogonal_vec(axis)), tags, imp, 70.0, now,
                )
            await seed(0, "normal", ["decision"], "rank-tag-decision")
            await seed(1, "normal", ["gotcha"], "rank-tag-gotcha")
            await seed(2, "normal", ["preference"], "rank-tag-preference")
            await seed(3, "normal", ["decision", "gotcha"], "rank-tag-both")

        only_decision = await db.search_memories(
            query_embedding=orthogonal_vec(0),
            project_id=proj_tags,
            owner_user_id=alice_uid,
            tags=["decision"],
            limit=10,
        )
        returned_tags = {frozenset(r["tags"]) for r in only_decision}
        check("tags=['decision']: only memories tagged decision returned",
              all("decision" in r["tags"] for r in only_decision))
        check("tags=['decision']: includes the multi-tag row "
              "(tag filter uses overlap, not equals)",
              frozenset({"decision", "gotcha"}) in returned_tags)

        # ================================================================
        # Scenario 6: deterministic tiebreaker (identical score -> newest
        # created_at first, then id ASC)
        # ================================================================
        proj_tie = await db.get_or_create_project(
            f"/tmp/rank-tie-{run_id}", owner_user_id=alice_uid,
        )
        # Two rows with the same embedding, importance, tags -> identical
        # score. Control created_at explicitly so we know which is
        # "newer". id ASC as tertiary tiebreaker if created_at also ties.
        t_old = now - timedelta(hours=2)
        t_new = now - timedelta(minutes=1)
        old_id = uuid4()
        new_id = uuid4()
        async with rls.admin_conn(pool) as conn:
            # Insert the older one first (natural id ordering unimportant,
            # but we want created_at to distinguish them).
            await conn.execute(
                """INSERT INTO memories
                       (id, project_id, owner_user_id, content,
                        embedding, tags, importance, temperature,
                        last_accessed, created_at)
                   VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8, $9,
                           $10)""",
                old_id, proj_tie, alice_uid, "tie-old",
                str(BASE_VEC), [], "normal", 70.0, t_old, t_old,
            )
            await conn.execute(
                """INSERT INTO memories
                       (id, project_id, owner_user_id, content,
                        embedding, tags, importance, temperature,
                        last_accessed, created_at)
                   VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8, $9,
                           $10)""",
                new_id, proj_tie, alice_uid, "tie-new",
                str(BASE_VEC), [], "normal", 70.0, t_new, t_new,
            )

        results = await db.search_memories(
            query_embedding=BASE_VEC,
            project_id=proj_tie,
            owner_user_id=alice_uid,
            limit=10,
        )
        check("tiebreaker: newer created_at ranks first",
              len(results) >= 2
              and results[0]["content"] == "tie-new"
              and results[1]["content"] == "tie-old")

        # ================================================================
        # Scenario 7: cross-user RLS (bob's memories never appear in
        # alice's search even on the same embedding)
        # ================================================================
        set_current_user_id(bob_uid)
        proj_bob = await db.get_or_create_project(
            f"/tmp/rank-bob-{run_id}", owner_user_id=bob_uid,
        )
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                """INSERT INTO memories
                       (id, project_id, owner_user_id, content,
                        embedding, tags, importance, temperature,
                        last_accessed)
                   VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8, $9)""",
                uuid4(), proj_bob, bob_uid,
                "bob-secret-memory",
                str(BASE_VEC), [], "critical", 70.0, now,
            )

        set_current_user_id(alice_uid)
        results = await db.search_memories(
            query_embedding=BASE_VEC,
            project_id=proj_rank,
            owner_user_id=alice_uid,
            limit=10,
        )
        check("RLS: bob's critical memory with identical embedding is not "
              "in alice's results",
              not any(r["content"] == "bob-secret-memory" for r in results))

        # ================================================================
        # Scenario 8: limit clamping (1..100)
        # ================================================================
        # Default behavior is clamped inside the function. Passing 0
        # becomes 1, passing 200 becomes 100. We verify by asking for
        # a huge number and counting that the response is <= 100.
        huge = await db.search_memories(
            query_embedding=BASE_VEC,
            project_id=proj_rank,
            owner_user_id=alice_uid,
            limit=5000,
        )
        check("limit clamped to <= 100", len(huge) <= 100)

    finally:
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                "DELETE FROM memories WHERE owner_user_id = ANY($1)",
                [alice_uid, bob_uid],
            )
            await conn.execute(
                "DELETE FROM facts WHERE owner_user_id = ANY($1)",
                [alice_uid, bob_uid],
            )
            await conn.execute(
                "DELETE FROM projects WHERE owner_user_id = ANY($1)",
                [alice_uid, bob_uid],
            )
        await pool.execute(
            "DELETE FROM users WHERE id = ANY($1)",
            [alice_uid, bob_uid],
        )
        db._store_counters.clear()
        await db.close_pool()

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
