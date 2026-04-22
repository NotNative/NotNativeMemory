"""
Integration tests for cap-enforcement eviction in lib/db._enforce_cap.

Eviction fires on every store (no throttle) once the project has more
than PROJECT_MEMORY_CAP (500) memories. The victims are chosen by:

    importance ASC (low evicts before critical),
    temperature ASC (coldest first within an importance tier),
    last_accessed ASC (least recently touched within a temp tier)

These tests cover:
- At or below cap: no eviction.
- Above cap: exactly (count - cap) victims.
- Importance as primary sort: low dies before high dies before critical.
- Temperature as secondary sort (same importance): coldest dies first.
- last_accessed as tertiary sort (same importance + temperature):
  least recently accessed dies first.
- All-critical project: 501 criticals still evicts one critical
  (documented behavior: we do not refuse the 501st store).

Uses direct-SQL seeding to fill each scenario project past the cap
without tripping the cooling path or the dedup path during setup.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_memory_eviction.py
"""

import asyncio
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib.embeddings import EMBEDDING_DIM


def orthogonal_vec(axis: int) -> list:
    v = [0.0] * EMBEDDING_DIM
    v[axis] = 1.0
    return v


async def _seed_memories(conn, project_id, owner_uid, rows):
    """Bulk insert memories with full control over temperature,
    importance, last_accessed. Each row is a dict with axis (for
    embedding), importance, temperature, last_accessed, tag."""
    payloads = []
    for r in rows:
        payloads.append((
            r.get("id") or uuid4(),
            project_id, owner_uid,
            r.get("content", f"eviction-seed-{r['axis']}"),
            str(orthogonal_vec(r["axis"])),
            r.get("tags", []),
            r["importance"], r["temperature"],
            r["last_accessed"],
        ))
    await conn.executemany(
        """INSERT INTO memories
               (id, project_id, owner_user_id, content,
                embedding, tags, importance, temperature,
                last_accessed)
           VALUES ($1, $2, $3, $4, $5::vector, $6, $7, $8, $9)""",
        payloads,
    )
    return [p[0] for p in payloads]


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
    test_username = f"eviction-test-{run_id}"

    pool = await db.get_pool()
    user = await auth_db.create_user(test_username, "password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)

    now = datetime.now(timezone.utc)

    try:
        # ================================================================
        # Scenario 1: at cap exactly -> no eviction fires
        # ================================================================
        proj_at_cap = await db.get_or_create_project(
            f"/tmp/eviction-at-cap-{run_id}", owner_user_id=uid,
        )
        db._store_counters.clear()

        # Seed 499 then store 1 via store_memory -> total 500 = cap.
        async with rls.admin_conn(pool) as conn:
            await _seed_memories(conn, proj_at_cap, uid, [
                {"axis": i, "importance": "normal",
                 "temperature": 50.0, "last_accessed": now}
                for i in range(499)
            ])

        await db.store_memory(
            content="final-at-cap", embedding=orthogonal_vec(499),
            project_id=proj_at_cap, owner_user_id=uid,
        )

        async with rls.admin_conn(pool) as conn:
            count_at_cap = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE project_id = $1",
                proj_at_cap,
            )
        check("at cap (500): no eviction fires",
              count_at_cap == db.PROJECT_MEMORY_CAP)

        # ================================================================
        # Scenario 2: one over cap -> exactly one evicted
        # ================================================================
        # Continue in the same project: we are at 500 now. Store one
        # more -> total 501, eviction fires, exactly 1 evicted.
        # The victim should be the coldest normal memory. All our
        # seeded normals share temperature 50.0, but the store we
        # just did left a new memory at TEMP_INITIAL (70.0), so the
        # coldest is one of the original 499.
        await db.store_memory(
            content="one-over-cap", embedding=orthogonal_vec(500),
            project_id=proj_at_cap, owner_user_id=uid,
        )

        async with rls.admin_conn(pool) as conn:
            count_one_over = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE project_id = $1",
                proj_at_cap,
            )
        check("one over cap (501): exactly one evicted (back to 500)",
              count_one_over == db.PROJECT_MEMORY_CAP)

        # ================================================================
        # Scenario 3: importance as primary sort (low dies first)
        # ================================================================
        proj_imp = await db.get_or_create_project(
            f"/tmp/eviction-imp-{run_id}", owner_user_id=uid,
        )
        db._store_counters.clear()

        # Seed 500 with distinct importances. Make the lowest-importance
        # rows not the coldest so the importance primary-sort is what
        # actually picks the victim.
        seeds = []
        low_id = uuid4()
        normal_id = uuid4()
        high_id = uuid4()
        critical_id = uuid4()
        seeds.append({"id": low_id, "axis": 600, "importance": "low",
                      "temperature": 90.0, "last_accessed": now})
        seeds.append({"id": normal_id, "axis": 601, "importance": "normal",
                      "temperature": 10.0, "last_accessed": now})
        seeds.append({"id": high_id, "axis": 602, "importance": "high",
                      "temperature": 10.0, "last_accessed": now})
        seeds.append({"id": critical_id, "axis": 603, "importance": "critical",
                      "temperature": 10.0, "last_accessed": now})
        # Fill to 500 with normals at mid-range temperature and recent
        # access so they are not candidates.
        for i in range(496):
            seeds.append({"axis": i, "importance": "normal",
                          "temperature": 60.0, "last_accessed": now})

        async with rls.admin_conn(pool) as conn:
            await _seed_memories(conn, proj_imp, uid, seeds)

        # Trigger eviction: one store past cap. The low-importance row
        # MUST be the victim, not the very-cold normal/high/critical.
        await db.store_memory(
            content="trigger-imp",
            embedding=orthogonal_vec(700),
            project_id=proj_imp, owner_user_id=uid,
        )

        async with rls.admin_conn(pool) as conn:
            survivors = set(r["id"] for r in await conn.fetch(
                "SELECT id FROM memories WHERE project_id = $1",
                proj_imp,
            ))
        check("importance primary: low evicted even though hottest",
              low_id not in survivors)
        check("importance primary: cold critical survived low",
              critical_id in survivors)
        check("importance primary: cold high survived low",
              high_id in survivors)
        check("importance primary: cold normal survived low",
              normal_id in survivors)

        # ================================================================
        # Scenario 4: temperature as secondary sort (coldest first
        # within the same importance tier)
        # ================================================================
        proj_temp = await db.get_or_create_project(
            f"/tmp/eviction-temp-{run_id}", owner_user_id=uid,
        )
        db._store_counters.clear()

        # All normal importance. Two "candidate" cold ones, rest warm.
        cold_victim = uuid4()
        warm_survivor = uuid4()
        seeds = [
            {"id": cold_victim, "axis": 10, "importance": "normal",
             "temperature": 5.0, "last_accessed": now},
            {"id": warm_survivor, "axis": 11, "importance": "normal",
             "temperature": 80.0, "last_accessed": now},
        ]
        for i in range(498):
            seeds.append({"axis": 100 + i, "importance": "normal",
                          "temperature": 40.0, "last_accessed": now})

        async with rls.admin_conn(pool) as conn:
            await _seed_memories(conn, proj_temp, uid, seeds)

        await db.store_memory(
            content="trigger-temp",
            embedding=orthogonal_vec(720),
            project_id=proj_temp, owner_user_id=uid,
        )

        async with rls.admin_conn(pool) as conn:
            survivors_temp = set(r["id"] for r in await conn.fetch(
                "SELECT id FROM memories WHERE project_id = $1",
                proj_temp,
            ))
        check("temperature secondary: coldest normal evicted first",
              cold_victim not in survivors_temp)
        check("temperature secondary: warmest normal survived",
              warm_survivor in survivors_temp)

        # ================================================================
        # Scenario 5: last_accessed as tertiary sort (same importance
        # + same temperature tier -> least recently accessed dies)
        # ================================================================
        proj_access = await db.get_or_create_project(
            f"/tmp/eviction-access-{run_id}", owner_user_id=uid,
        )
        db._store_counters.clear()

        ancient_id = uuid4()
        recent_id = uuid4()
        ancient = now - timedelta(days=30)
        very_recent = now - timedelta(minutes=1)
        seeds = [
            {"id": ancient_id, "axis": 30, "importance": "normal",
             "temperature": 20.0, "last_accessed": ancient},
            {"id": recent_id, "axis": 31, "importance": "normal",
             "temperature": 20.0, "last_accessed": very_recent},
        ]
        # Fill with warmer normals so the above two are the candidates.
        for i in range(498):
            seeds.append({"axis": 200 + i, "importance": "normal",
                          "temperature": 60.0, "last_accessed": now})

        async with rls.admin_conn(pool) as conn:
            await _seed_memories(conn, proj_access, uid, seeds)

        await db.store_memory(
            content="trigger-access",
            embedding=orthogonal_vec(730),
            project_id=proj_access, owner_user_id=uid,
        )

        async with rls.admin_conn(pool) as conn:
            survivors_acc = set(r["id"] for r in await conn.fetch(
                "SELECT id FROM memories WHERE project_id = $1",
                proj_access,
            ))
        check("last_accessed tertiary: oldest-accessed evicted first",
              ancient_id not in survivors_acc)
        check("last_accessed tertiary: more recent one survived",
              recent_id in survivors_acc)

        # ================================================================
        # Scenario 6: all-critical project evicts a critical on overflow
        # ================================================================
        # Documented behavior: we do not refuse the 501st critical.
        # Eviction proceeds with the normal sort (importance, temp,
        # last_accessed). Since every row is critical, it becomes a
        # temperature+last_accessed decision among criticals.
        proj_all_crit = await db.get_or_create_project(
            f"/tmp/eviction-allcrit-{run_id}", owner_user_id=uid,
        )
        db._store_counters.clear()

        coldest_crit = uuid4()
        warmer_crit = uuid4()
        seeds = [
            {"id": coldest_crit, "axis": 40, "importance": "critical",
             "temperature": 50.0, "last_accessed": now},
            {"id": warmer_crit, "axis": 41, "importance": "critical",
             "temperature": 95.0, "last_accessed": now},
        ]
        for i in range(498):
            seeds.append({"axis": 50 + i, "importance": "critical",
                          "temperature": 80.0, "last_accessed": now})

        async with rls.admin_conn(pool) as conn:
            await _seed_memories(conn, proj_all_crit, uid, seeds)

        await db.store_memory(
            content="trigger-allcrit",
            embedding=orthogonal_vec(740),
            project_id=proj_all_crit, owner_user_id=uid,
            importance="critical",
        )

        async with rls.admin_conn(pool) as conn:
            crit_count = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE project_id = $1",
                proj_all_crit,
            )
            survivors_crit = set(r["id"] for r in await conn.fetch(
                "SELECT id FROM memories WHERE project_id = $1",
                proj_all_crit,
            ))
        check("all-critical: 501st critical still evicts one critical",
              crit_count == db.PROJECT_MEMORY_CAP)
        check("all-critical: coldest critical is the victim",
              coldest_crit not in survivors_crit)
        check("all-critical: warmest critical survives",
              warmer_crit in survivors_crit)

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
