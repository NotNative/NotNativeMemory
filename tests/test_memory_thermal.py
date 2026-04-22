"""
Integration tests for thermal mechanics in lib/db.

Covers the six thermal invariants:

1. Fresh stores land at TEMP_INITIAL (70.0).
2. Displacement cooling is throttled to every _MIN_STORES_BETWEEN_COOL
   (3) stores per project.
3. Per-importance cool rates: critical=0, high=0.25x, normal=1x,
   low=2x (multiplied by DISPLACEMENT_COOL_DELTA = 0.5).
4. Critical memories never cool.
5. Pressure cooling: when project fill >= 80% of cap, an extra
   PRESSURE_COOL_DELTA (0.5) is added to the base cool.
6. Temperature floor: cooling clamps at 0 via GREATEST(... , 0.0);
   reheating on search caps at TEMP_MAX via LEAST(... , TEMP_MAX).

Uses orthogonal unit vectors so distinct stores never trip dedup
(pairwise cosine similarity is exactly 0).

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_memory_thermal.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID, uuid4

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib.embeddings import EMBEDDING_DIM


def orthogonal_vec(axis: int) -> list:
    """Unit vector pointing along a single dimension. Pairwise cosine
    similarity between two orthogonal_vec calls is exactly 0, so no
    store ever trips dedup against another stored via this helper."""
    v = [0.0] * EMBEDDING_DIM
    v[axis] = 1.0
    return v


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
    test_username = f"thermal-test-{run_id}"

    pool = await db.get_pool()
    user = await auth_db.create_user(test_username, "password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)

    try:
        # ================================================================
        # Scenario 1: TEMP_INITIAL + throttle behavior
        # ================================================================
        proj_throttle = await db.get_or_create_project(
            f"/tmp/thermal-throttle-{run_id}", owner_user_id=uid,
        )
        db._store_counters.clear()

        # Store A: counter goes 0 -> 1, no cool
        mem_a = await db.store_memory(
            content="throttle-A", embedding=orthogonal_vec(0),
            project_id=proj_throttle, owner_user_id=uid,
            importance="normal",
        )
        async with rls.admin_conn(pool) as conn:
            temp_a_initial = await conn.fetchval(
                "SELECT temperature FROM memories WHERE id = $1", mem_a,
            )
        check("fresh store lands at TEMP_INITIAL (70.0)",
              abs(float(temp_a_initial) - db.TEMP_INITIAL) < 0.01)

        # Store B: counter 1 -> 2, still no cool. A unchanged.
        mem_b = await db.store_memory(
            content="throttle-B", embedding=orthogonal_vec(1),
            project_id=proj_throttle, owner_user_id=uid,
            importance="normal",
        )
        async with rls.admin_conn(pool) as conn:
            temp_a_after_b = await conn.fetchval(
                "SELECT temperature FROM memories WHERE id = $1", mem_a,
            )
        check("after 2nd store: throttle blocks cooling (A still 70)",
              abs(float(temp_a_after_b) - 70.0) < 0.01)

        # Store C: counter 2 -> 3, cool fires, counter resets to 0.
        # All three normals in project cool by 1.0 * 0.5 = 0.5.
        mem_c = await db.store_memory(
            content="throttle-C", embedding=orthogonal_vec(2),
            project_id=proj_throttle, owner_user_id=uid,
            importance="normal",
        )
        async with rls.admin_conn(pool) as conn:
            rows = await conn.fetch(
                """SELECT id, temperature FROM memories
                   WHERE project_id = $1""",
                proj_throttle,
            )
        temps = {r["id"]: float(r["temperature"]) for r in rows}
        check("after 3rd store: A cooled 70 -> 69.5",
              abs(temps[mem_a] - 69.5) < 0.01)
        check("after 3rd store: B cooled 70 -> 69.5",
              abs(temps[mem_b] - 69.5) < 0.01)
        check("after 3rd store: C cooled 70 -> 69.5 (new row cools too)",
              abs(temps[mem_c] - 69.5) < 0.01)

        # ================================================================
        # Scenario 2: per-importance cooling rates
        # ================================================================
        proj_rates = await db.get_or_create_project(
            f"/tmp/thermal-rates-{run_id}", owner_user_id=uid,
        )
        db._store_counters.clear()

        # Seed the four importance tiers directly via SQL so all four
        # predate the first cool cycle and experience every cycle
        # equally. Using store_memory here would trip a mid-setup cool.
        mems = {}
        async with rls.admin_conn(pool) as conn:
            for i, imp in enumerate(["critical", "high", "normal", "low"]):
                row_id = uuid4()
                mems[imp] = row_id
                await conn.execute(
                    """INSERT INTO memories
                           (id, project_id, owner_user_id, content,
                            embedding, tags, importance, temperature)
                       VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8)""",
                    row_id, proj_rates, uid, f"rate-{imp}",
                    str(orthogonal_vec(10 + i)), [], imp, 70.0,
                )

        # Now drive exactly one cool cycle via three store_memory calls
        # (counter 0 -> 1 -> 2 -> 3-cool-reset).
        for i in range(3):
            await db.store_memory(
                content=f"rate-pad-{i}", embedding=orthogonal_vec(20 + i),
                project_id=proj_rates, owner_user_id=uid,
                importance="normal",
            )

        async with rls.admin_conn(pool) as conn:
            temp_rows = await conn.fetch(
                "SELECT id, importance, temperature FROM memories "
                "WHERE project_id = $1 AND id = ANY($2)",
                proj_rates, list(mems.values()),
            )
        imp_to_temp = {r["importance"]: float(r["temperature"])
                       for r in temp_rows}

        # One cool cycle fired after the 3rd pad.
        # Critical: 0 rate, no drop, stays 70.0
        # High: 0.25 * 0.5 = 0.125 drop, 70.0 -> 69.875
        # Normal: 1.0 * 0.5 = 0.5 drop, 70.0 -> 69.5
        # Low: 2.0 * 0.5 = 1.0 drop, 70.0 -> 69.0
        check("critical: no cooling (0 rate)",
              abs(imp_to_temp["critical"] - 70.0) < 0.01)
        check("high: 0.25x cool rate (70 -> 69.875)",
              abs(imp_to_temp["high"] - 69.875) < 0.01)
        check("normal: 1.0x cool rate (70 -> 69.5)",
              abs(imp_to_temp["normal"] - 69.5) < 0.01)
        check("low: 2.0x cool rate (70 -> 69.0)",
              abs(imp_to_temp["low"] - 69.0) < 0.01)

        # ================================================================
        # Scenario 3: temperature floor (GREATEST(... , 0.0))
        # ================================================================
        proj_floor = await db.get_or_create_project(
            f"/tmp/thermal-floor-{run_id}", owner_user_id=uid,
        )
        db._store_counters.clear()

        floor_victim_id = uuid4()
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                """INSERT INTO memories
                       (id, project_id, owner_user_id, content,
                        embedding, tags, importance, temperature)
                   VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8)""",
                floor_victim_id, proj_floor, uid, "floor-victim",
                str(orthogonal_vec(30)), [], "low", 0.3,
            )

        # Three stores with counter starting at 0 -> cool on the third.
        # Low cool delta = 2.0 * 0.5 = 1.0. Victim at 0.3 - 1.0 would be
        # -0.7, but GREATEST clamps to 0.0.
        for i in range(3):
            await db.store_memory(
                content=f"floor-pad-{i}",
                embedding=orthogonal_vec(31 + i),
                project_id=proj_floor, owner_user_id=uid,
                importance="normal",
            )

        async with rls.admin_conn(pool) as conn:
            temp_floor = await conn.fetchval(
                "SELECT temperature FROM memories WHERE id = $1",
                floor_victim_id,
            )
        check("temperature floor clamps at 0.0 (did not go negative)",
              float(temp_floor) == 0.0)

        # ================================================================
        # Scenario 4: pressure cooling at 80% fill
        # ================================================================
        proj_pressure = await db.get_or_create_project(
            f"/tmp/thermal-pressure-{run_id}", owner_user_id=uid,
        )
        db._store_counters.clear()

        # Seed the victim. Temperature = 70.0, importance = normal.
        pressure_victim_id = uuid4()
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                """INSERT INTO memories
                       (id, project_id, owner_user_id, content,
                        embedding, tags, importance, temperature)
                   VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8)""",
                pressure_victim_id, proj_pressure, uid, "pressure-victim",
                str(orthogonal_vec(100)), [], "normal", 70.0,
            )

            # Bulk-insert 399 filler memories directly so no cooling
            # fires during setup. 1 victim + 399 filler = 400 rows =
            # 80% of cap exactly. The threshold is >= 0.8, so pressure
            # kicks in on the next store_memory call.
            filler_rows = []
            for i in range(399):
                filler_rows.append((
                    uuid4(), proj_pressure, uid, f"filler-{i}",
                    str(orthogonal_vec(200 + i)), [], "normal", 70.0,
                ))
            await conn.executemany(
                """INSERT INTO memories
                       (id, project_id, owner_user_id, content,
                        embedding, tags, importance, temperature)
                   VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8)""",
                filler_rows,
            )

        # Now trip cooling. Three real stores: first two increment
        # counter to 1 and 2 (no cool), third triggers cooling.
        for i in range(3):
            await db.store_memory(
                content=f"pressure-trigger-{i}",
                embedding=orthogonal_vec(600 + i),
                project_id=proj_pressure, owner_user_id=uid,
                importance="normal",
            )

        async with rls.admin_conn(pool) as conn:
            temp_pressure = await conn.fetchval(
                "SELECT temperature FROM memories WHERE id = $1",
                pressure_victim_id,
            )
        # At >=80% fill: cool = (DISPLACEMENT_COOL_DELTA +
        # PRESSURE_COOL_DELTA) * _COOL_RATE['normal'] = (0.5 + 0.5) * 1.0
        # = 1.0. Victim 70.0 -> 69.0.
        check("pressure cooling: at 80% fill, normal drops by 1.0 not 0.5",
              abs(float(temp_pressure) - 69.0) < 0.01)

        # ================================================================
        # Scenario 5: search reheats the returned memories
        # ================================================================
        proj_reheat = await db.get_or_create_project(
            f"/tmp/thermal-reheat-{run_id}", owner_user_id=uid,
        )
        db._store_counters.clear()

        # Seed a memory at temperature 50 so we can observe the bump.
        reheat_id = uuid4()
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                """INSERT INTO memories
                       (id, project_id, owner_user_id, content,
                        embedding, tags, importance, temperature)
                   VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8)""",
                reheat_id, proj_reheat, uid, "reheat-target",
                str(orthogonal_vec(500)), [], "normal", 50.0,
            )

        # Search for the same vector. Should return the target and
        # reheat it by REHEAT_DELTA (10.0) capped at TEMP_MAX (95.0).
        results = await db.search_memories(
            query_embedding=orthogonal_vec(500),
            project_id=proj_reheat,
            owner_user_id=uid,
            limit=5,
        )
        check("search returned the seeded memory", len(results) == 1)
        check("search result similarity is 1.0 (exact vector match)",
              results and abs(results[0]["similarity"] - 1.0) < 0.01)

        async with rls.admin_conn(pool) as conn:
            reheated = await conn.fetchval(
                "SELECT temperature FROM memories WHERE id = $1", reheat_id,
            )
        check("search reheats accessed memory (50 -> 60 after +10.0)",
              abs(float(reheated) - 60.0) < 0.01)

        # Second search should bump to 70, not 60.01 -- contract is
        # +REHEAT_DELTA each time, not a one-shot increment.
        await db.search_memories(
            query_embedding=orthogonal_vec(500),
            project_id=proj_reheat,
            owner_user_id=uid,
            limit=5,
        )
        async with rls.admin_conn(pool) as conn:
            reheated2 = await conn.fetchval(
                "SELECT temperature FROM memories WHERE id = $1", reheat_id,
            )
        check("second search reheats again (60 -> 70)",
              abs(float(reheated2) - 70.0) < 0.01)

        # Reheat cap: push a memory to near TEMP_MAX and verify further
        # searches clamp at TEMP_MAX rather than overshooting.
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                "UPDATE memories SET temperature = 90.0 WHERE id = $1",
                reheat_id,
            )
        await db.search_memories(
            query_embedding=orthogonal_vec(500),
            project_id=proj_reheat,
            owner_user_id=uid,
            limit=5,
        )
        async with rls.admin_conn(pool) as conn:
            capped = await conn.fetchval(
                "SELECT temperature FROM memories WHERE id = $1", reheat_id,
            )
        check("reheat capped at TEMP_MAX (90 + 10 -> 95 not 100)",
              abs(float(capped) - db.TEMP_MAX) < 0.01)

    finally:
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                "DELETE FROM memories WHERE owner_user_id = $1", uid,
            )
            await conn.execute(
                "DELETE FROM projects WHERE owner_user_id = $1", uid,
            )
        await pool.execute("DELETE FROM users WHERE id = $1", uid)
        db._store_counters.clear()
        await db.close_pool()

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
