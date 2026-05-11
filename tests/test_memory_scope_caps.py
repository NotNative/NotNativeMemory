"""
Integration tests for per-scope memory caps in lib/db.

The eviction cap depends on the project's scope:
  local  -> PROJECT_MEMORY_CAP
  domain -> DOMAIN_MEMORY_CAP
  global -> GLOBAL_MEMORY_CAP

Tests use _seed_memories from test_memory_eviction.py to fill projects
past a small monkeypatched cap, then assert eviction trims back to
that scope's cap.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_memory_scope_caps.py
"""

import asyncio
import os
import secrets
import sys
from datetime import datetime, timezone
from uuid import UUID, uuid4

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib.embeddings import EMBEDDING_DIM

sys.path.insert(0, HERE)
from test_memory_eviction import _seed_memories, orthogonal_vec  # noqa: E402


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

    # Unit: pure scope -> cap mapping
    check("_cap_for_scope('local') == PROJECT_MEMORY_CAP",
          db._cap_for_scope("local") == db.PROJECT_MEMORY_CAP)
    check("_cap_for_scope('domain') == DOMAIN_MEMORY_CAP",
          db._cap_for_scope("domain") == db.DOMAIN_MEMORY_CAP)
    check("_cap_for_scope('global') == GLOBAL_MEMORY_CAP",
          db._cap_for_scope("global") == db.GLOBAL_MEMORY_CAP)
    check("_cap_for_scope(None) falls back to PROJECT_MEMORY_CAP",
          db._cap_for_scope(None) == db.PROJECT_MEMORY_CAP)
    check("_cap_for_scope('unknown') falls back to PROJECT_MEMORY_CAP",
          db._cap_for_scope("unknown") == db.PROJECT_MEMORY_CAP)

    # Defaults: domain and global are larger than project
    check("DOMAIN_MEMORY_CAP > PROJECT_MEMORY_CAP by default",
          db.DOMAIN_MEMORY_CAP > db.PROJECT_MEMORY_CAP)
    check("GLOBAL_MEMORY_CAP > PROJECT_MEMORY_CAP by default",
          db.GLOBAL_MEMORY_CAP > db.PROJECT_MEMORY_CAP)

    # Integration: monkeypatch tiny caps so the test runs fast.
    # Save originals to restore in finally.
    original_caps = dict(db._CAP_BY_SCOPE)
    test_caps = {"local": 3, "domain": 5, "global": 5}
    db._CAP_BY_SCOPE.clear()
    db._CAP_BY_SCOPE.update(test_caps)

    run_id = secrets.token_hex(4)
    test_username = f"scope-cap-test-{run_id}"

    pool = await db.get_pool()
    user = await auth_db.create_user(test_username, "password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)

    now = datetime.now(timezone.utc)

    try:
        # local-scope project: cap=3
        proj_local = await db.get_or_create_project(
            f"/tmp/scope-local-{run_id}", owner_user_id=uid,
        )
        # domain-scope project: cap=5
        proj_domain = await db.get_or_create_project(
            f"_domain_scopecaps_{run_id}", owner_user_id=uid,
        )
        # global-scope project: cap=5 (one _global per user)
        proj_global = await db.get_or_create_project(
            "_global", owner_user_id=uid,
        )

        # Each project gets seeded one row under its cap, then a final
        # store via store_memory pushes past the cap by 2 so eviction
        # must fire.
        async with rls.admin_conn(pool) as conn:
            # local: cap=3, seed 3 -> store one more -> 4 -> evict to 3
            await _seed_memories(conn, proj_local, uid, [
                {"axis": 5000 + i, "importance": "normal",
                 "temperature": 50.0, "last_accessed": now}
                for i in range(3)
            ])
            # domain: cap=5, seed 5 -> store one more -> 6 -> evict to 5
            await _seed_memories(conn, proj_domain, uid, [
                {"axis": 6000 + i, "importance": "normal",
                 "temperature": 50.0, "last_accessed": now}
                for i in range(5)
            ])
            # global: cap=5, seed 5 -> store one more -> 6 -> evict to 5
            await _seed_memories(conn, proj_global, uid, [
                {"axis": 7000 + i, "importance": "normal",
                 "temperature": 50.0, "last_accessed": now}
                for i in range(5)
            ])

        db._store_counters.clear()
        await db.store_memory(
            content="local-trigger", embedding=orthogonal_vec(5999),
            project_id=proj_local, owner_user_id=uid,
        )
        await db.store_memory(
            content="domain-trigger", embedding=orthogonal_vec(6999),
            project_id=proj_domain, owner_user_id=uid,
        )
        await db.store_memory(
            content="global-trigger", embedding=orthogonal_vec(7999),
            project_id=proj_global, owner_user_id=uid,
        )

        async with rls.admin_conn(pool) as conn:
            local_count = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE project_id = $1",
                proj_local,
            )
            domain_count = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE project_id = $1",
                proj_domain,
            )
            global_count = await conn.fetchval(
                "SELECT count(*) FROM memories WHERE project_id = $1",
                proj_global,
            )

        check(f"local cap=3 enforced after store (count={local_count})",
              local_count == 3)
        check(f"domain cap=5 enforced after store (count={domain_count})",
              domain_count == 5)
        check(f"global cap=5 enforced after store (count={global_count})",
              global_count == 5)

        # Cross-check: domain project at local-cap (3) would have over-
        # evicted. Verify the eviction respected the larger domain cap
        # by checking we still have more than PROJECT cap=3 rows there.
        check("domain scope did NOT use local cap (would have left 3)",
              domain_count > test_caps["local"])

    finally:
        # restore original caps before any other test runs
        db._CAP_BY_SCOPE.clear()
        db._CAP_BY_SCOPE.update(original_caps)

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
