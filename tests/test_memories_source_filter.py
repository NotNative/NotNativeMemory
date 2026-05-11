"""
Tests for the source filter on the /memories admin page.

Two surfaces:
  - lib/db.admin_list_memories now accepts a `source_kind` filter. The
    SQL whitelisting protects against injection via a hostile query
    string value.
  - templates/memories.html exposes a dropdown so curators can split
    analyzer-extracted memories (source=model-inferred) from
    user-stated rules without scanning by eye.

Integration test seeds memories with each known source value and
verifies the filter returns the correct subset, plus a static template
check so the dropdown can't quietly disappear.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_memories_source_filter.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


def _static_template_checks() -> int:
    """Smoke-check the template embeds the source_kind dropdown."""
    failed = 0
    path = os.path.join(ROOT, "templates", "memories.html")
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    needles = [
        'name="source_kind"',
        '>source: any<',
        '>user-stated<',
        '>tool-result<',
        '>model-inferred<',
        "filters.source_kind == 'user-stated'",
        "filters.source_kind == 'tool-result'",
        "filters.source_kind == 'model-inferred'",
    ]
    for n in needles:
        ok = n in html
        print(f"  {'PASS' if ok else 'FAIL'}  template contains {n!r}")
        if not ok:
            failed += 1
    return failed


async def _db_filter_checks() -> int:
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id
    from lib.embeddings import EMBEDDING_DIM

    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    run_id = secrets.token_hex(4)
    user = await auth_db.create_user(f"src-filter-{run_id}", "password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)
    pool = await db.get_pool()

    try:
        project_id = await db.get_or_create_project(
            f"/tmp/src-filter-{run_id}", owner_user_id=uid,
        )

        # Seed one row per source_kind plus an unattributed row.
        def vec(axis: int):
            v = [0.0] * EMBEDDING_DIM
            v[axis] = 1.0
            return v

        await db.store_memory(
            content=f"user-said-{run_id}", embedding=vec(0),
            project_id=project_id, owner_user_id=uid,
            source_kind="user-stated",
        )
        await db.store_memory(
            content=f"from-tool-{run_id}", embedding=vec(1),
            project_id=project_id, owner_user_id=uid,
            source_kind="tool-result",
        )
        await db.store_memory(
            content=f"model-said-{run_id}", embedding=vec(2),
            project_id=project_id, owner_user_id=uid,
            source_kind="model-inferred",
        )
        await db.store_memory(
            content=f"no-source-{run_id}", embedding=vec(3),
            project_id=project_id, owner_user_id=uid,
        )  # source_kind defaults to NULL

        # No filter -> 4 rows.
        rows, total = await db.admin_list_memories(
            owner_user_id=uid, project=f"/tmp/src-filter-{run_id}",
        )
        check(f"no filter returns 4 rows (got {total})", total == 4)

        # user-stated -> 1 row.
        rows, total = await db.admin_list_memories(
            owner_user_id=uid, project=f"/tmp/src-filter-{run_id}",
            source_kind="user-stated",
        )
        check(f"source_kind=user-stated returns 1 row (got {total})", total == 1)
        check("user-stated row content matches",
              total == 1 and rows[0]["content"] == f"user-said-{run_id}")

        # tool-result -> 1 row.
        rows, total = await db.admin_list_memories(
            owner_user_id=uid, project=f"/tmp/src-filter-{run_id}",
            source_kind="tool-result",
        )
        check(f"source_kind=tool-result returns 1 row (got {total})", total == 1)

        # model-inferred -> 1 row.
        rows, total = await db.admin_list_memories(
            owner_user_id=uid, project=f"/tmp/src-filter-{run_id}",
            source_kind="model-inferred",
        )
        check(f"source_kind=model-inferred returns 1 row (got {total})", total == 1)

        # Unknown values must be ignored (whitelist), NOT cause an error
        # and NOT inject into the SQL. Treat as "no filter" -> 4 rows.
        rows, total = await db.admin_list_memories(
            owner_user_id=uid, project=f"/tmp/src-filter-{run_id}",
            source_kind="not-a-real-source",
        )
        check(f"unknown source_kind falls back to no-filter (got {total})",
              total == 4)

        # Empty string -> no filter, same as None.
        rows, total = await db.admin_list_memories(
            owner_user_id=uid, project=f"/tmp/src-filter-{run_id}",
            source_kind="",
        )
        check(f"empty source_kind falls back to no-filter (got {total})",
              total == 4)

        # SQL-injection attempt -> treated as unknown, no filter applied.
        rows, total = await db.admin_list_memories(
            owner_user_id=uid, project=f"/tmp/src-filter-{run_id}",
            source_kind="user-stated' OR '1'='1",
        )
        check(f"injection attempt returns no-filter result (got {total})",
              total == 4)

    finally:
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                "DELETE FROM memories WHERE owner_user_id = $1", uid,
            )
            await conn.execute(
                "DELETE FROM projects WHERE owner_user_id = $1", uid,
            )
        await pool.execute("DELETE FROM users WHERE id = $1", uid)
        await db.close_pool()

    return failed


async def main() -> int:
    print("=== static template checks ===")
    failed_static = _static_template_checks()
    print()
    print("=== db filter integration ===")
    failed_db = await _db_filter_checks()
    total_failed = failed_static + failed_db
    print("---")
    print("all passed" if total_failed == 0 else f"{total_failed} FAILED")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
