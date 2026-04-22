"""
Integration tests for memory deduplication in lib/db.store_memory.

Exercises the dedup branch end-to-end against a live Postgres:
- Cosine similarity above DEDUP_SIMILARITY_THRESHOLD (0.92) merges
  into the existing memory instead of creating a duplicate row.
- Cosine below threshold creates a new memory.
- Dedup is scoped per project: identical vectors in two different
  projects stay as two rows.
- Merge semantics: content and embedding replaced, tags unioned,
  importance only upgrades, temperature reheats (capped at TEMP_MAX),
  access_count and created_at preserved.

Vectors are hand-crafted 2D rotations in the first two dimensions so
we can control the cosine similarity precisely. Unit vectors keep the
denominator in cosine distance equal to 1, so the similarity reported
by pgvector matches the theoretical cos(angle).

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_memory_dedup.py
"""

import asyncio
import math
import os
import secrets
import sys
from uuid import UUID

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib.embeddings import EMBEDDING_DIM


def vec_at_similarity(cos_sim: float) -> list:
    """
    Construct a unit vector whose cosine similarity to the canonical
    base vector [1, 0, 0, ...] equals cos_sim.

    The base vector is implied and not constructed here. Callers pair
    this with BASE_VEC below.
    """
    cos_sim = max(-1.0, min(1.0, cos_sim))
    angle = math.acos(cos_sim)
    v = [0.0] * EMBEDDING_DIM
    v[0] = math.cos(angle)
    v[1] = math.sin(angle)
    return v


BASE_VEC = vec_at_similarity(1.0)  # [1.0, 0.0, 0.0, ..., 0.0]


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

    # Isolate this run so a prior failed run's rows don't interfere.
    run_id = secrets.token_hex(4)
    test_username = f"dedup-test-{run_id}"

    pool = await db.get_pool()
    user = await auth_db.create_user(test_username, "password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)

    project_a_dir = f"/tmp/dedup-test-A-{run_id}"
    project_b_dir = f"/tmp/dedup-test-B-{run_id}"
    project_c_dir = f"/tmp/dedup-test-C-{run_id}"

    try:
        project_a = await db.get_or_create_project(project_a_dir, owner_user_id=uid)
        project_b = await db.get_or_create_project(project_b_dir, owner_user_id=uid)
        # Project C gets a fresh row for the below-threshold test so the
        # repeated merges in project A (which overwrite the embedding)
        # cannot influence the neighbor computation.
        project_c = await db.get_or_create_project(project_c_dir, owner_user_id=uid)

        # Reset the module-level store counter so pre-existing test state
        # cannot influence throttled cooling paths that piggyback on store.
        db._store_counters.clear()

        # -- Scenario 1: well above threshold merges -----------------------
        # 0.99 is clearly above 0.92 with plenty of float4 headroom.
        mem1 = await db.store_memory(
            content="First version of the memory",
            embedding=BASE_VEC,
            project_id=project_a,
            owner_user_id=uid,
            tags=["alpha"],
            importance="normal",
        )
        mem2 = await db.store_memory(
            content="Second version, nearly identical",
            embedding=vec_at_similarity(0.99),
            project_id=project_a,
            owner_user_id=uid,
            tags=["beta"],
            importance="high",
        )
        check("0.99 similarity merges (same UUID returned)", mem1 == mem2)

        async with rls.admin_conn(pool) as conn:
            count = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE project_id = $1",
                project_a,
            )
            row = await conn.fetchrow(
                """SELECT content, tags, importance, temperature,
                          access_count, created_at
                   FROM memories WHERE id = $1""",
                mem1,
            )
        check("after merge: only one row in project", count == 1)
        check("merge: content replaced with new value",
              row["content"] == "Second version, nearly identical")
        check("merge: tag union contains both tags",
              set(row["tags"]) >= {"alpha", "beta"})
        check("merge: importance upgraded normal -> high",
              row["importance"] == "high")
        # TEMP_INITIAL=70, REHEAT_DELTA=10, so after one merge: 80
        check("merge: temperature reheated to 80.0",
              abs(float(row["temperature"]) - 80.0) < 0.01)
        check("merge: access_count preserved (not incremented on merge)",
              row["access_count"] == 0)

        # -- Scenario 2: importance never downgrades on merge --------------
        mem3 = await db.store_memory(
            content="Downgrade attempt",
            embedding=vec_at_similarity(0.98),
            project_id=project_a,
            owner_user_id=uid,
            importance="low",
        )
        check("downgrade attempt merged into same row", mem3 == mem1)

        async with rls.admin_conn(pool) as conn:
            imp = await conn.fetchval(
                "SELECT importance FROM memories WHERE id = $1", mem1,
            )
        check("merge: importance stayed 'high' (not downgraded to 'low')",
              imp == "high")

        # -- Scenario 3: temperature capped at TEMP_MAX --------------------
        # Three more merges will try to push temperature past 95.0
        for _ in range(5):
            await db.store_memory(
                content=f"Heating merge {secrets.token_hex(2)}",
                embedding=vec_at_similarity(0.97),
                project_id=project_a,
                owner_user_id=uid,
            )
        async with rls.admin_conn(pool) as conn:
            temp = await conn.fetchval(
                "SELECT temperature FROM memories WHERE id = $1", mem1,
            )
        check("merge: temperature capped at TEMP_MAX (95.0)",
              float(temp) <= db.TEMP_MAX + 0.01)

        # -- Scenario 4: below-threshold similarity creates a new row ------
        # Using a fresh project so the repeated merges above do not alter
        # the neighbor vector (each merge overwrites embedding). We seed
        # project C with BASE_VEC and then store a 0.85-sim vector.
        mem_c_seed = await db.store_memory(
            content="Seed for below-threshold test",
            embedding=BASE_VEC,
            project_id=project_c,
            owner_user_id=uid,
        )
        mem_below = await db.store_memory(
            content="A different idea entirely",
            embedding=vec_at_similarity(0.85),
            project_id=project_c,
            owner_user_id=uid,
        )
        check("0.85 similarity did NOT merge (different UUID)",
              mem_below != mem_c_seed)

        async with rls.admin_conn(pool) as conn:
            count_c = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE project_id = $1",
                project_c,
            )
        check("below threshold: project C has two rows", count_c == 2)

        # -- Scenario 5: per-project isolation -----------------------------
        # Store a BASE_VEC memory in project B. Since project B is
        # otherwise empty, no dedup can happen and the same vector that
        # merged in project A (before repeated merges overwrote it)
        # stays as its own row here.
        mem_other_project = await db.store_memory(
            content="Identical vector, different project",
            embedding=BASE_VEC,
            project_id=project_b,
            owner_user_id=uid,
        )
        check("BASE_VEC in project B creates new row (not merged with A)",
              mem_other_project != mem1 and mem_other_project != mem_c_seed)

        async with rls.admin_conn(pool) as conn:
            count_b = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE project_id = $1",
                project_b,
            )
        check("project B has one row (not merged across projects)",
              count_b == 1)

        # -- Scenario 6: just-below-threshold boundary ---------------------
        # 0.91 is below 0.92 and should not merge. Well above float4 noise.
        mem_boundary = await db.store_memory(
            content="Boundary: just below threshold",
            embedding=vec_at_similarity(0.91),
            project_id=project_b,
            owner_user_id=uid,
        )
        check("0.91 similarity did NOT merge", mem_boundary != mem_other_project)

        # -- Scenario 7: just-above-threshold boundary ---------------------
        # 0.93 is safely above 0.92 even after float4 round-trip.
        mem_above = await db.store_memory(
            content="Boundary: just above threshold",
            embedding=vec_at_similarity(0.93),
            project_id=project_b,
            owner_user_id=uid,
        )
        check("0.93 similarity DID merge (into closest neighbor)",
              mem_above in (mem_other_project, mem_boundary))

    finally:
        # Cleanup. Deleting the user cascades via ON DELETE CASCADE, but
        # we explicitly remove the memory and project rows first as a
        # belt-and-suspenders against schema drift.
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
