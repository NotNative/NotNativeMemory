"""
Integration tests for source attribution (#7) and memory health (#9).

Exercises:
- Store with source_kind, verify it persists and surfaces in search results
- Store with invalid source_kind, verify rejection
- Health stats return correct counts by class, importance, source
- Health stats reflect never-accessed and fact counts

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_source_and_health.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID

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
    user_name = f"src-health-{run_id}"

    pool = await db.get_pool()
    user = await auth_db.create_user(user_name, "password-1234")
    user_uid = UUID(user["id"])
    set_current_user_id(user_uid)

    project_dir = f"/tmp/src-health-{run_id}"
    project_id = await db.get_or_create_project(project_dir, owner_user_id=user_uid)

    # --- Source attribution: store with source_kind ---
    emb1 = embed("The API runs on port 8080")
    mid1 = await db.store_memory(
        content="The API runs on port 8080",
        embedding=emb1,
        project_id=project_id,
        owner_user_id=user_uid,
        importance="normal",
        source_kind="user-stated",
        source_session_id="session-abc-123",
    )
    check("store with source_kind returns UUID", mid1 is not None)

    # Verify source fields persisted
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT source_kind, source_session_id FROM memories WHERE id = $1",
            mid1,
        )
    check("source_kind persisted", row["source_kind"] == "user-stated")
    check("source_session_id persisted", row["source_session_id"] == "session-abc-123")

    # Verify source surfaces in search results
    results = await db.search_memories(
        query_embedding=emb1,
        project_id=project_id,
        owner_user_id=user_uid,
        limit=5,
    )
    found = [r for r in results if r["id"] == str(mid1)]
    check("search result includes source_kind", len(found) == 1 and found[0]["source_kind"] == "user-stated")
    check("search result includes source_session_id", found[0]["source_session_id"] == "session-abc-123")

    # --- Store with invalid source_kind ---
    try:
        await db.store_memory(
            content="bad source",
            embedding=embed("bad source"),
            project_id=project_id,
            owner_user_id=user_uid,
            source_kind="invented",
        )
        check("invalid source_kind raises", False)
    except ValueError:
        check("invalid source_kind raises", True)

    # --- Store without source (NULL) ---
    emb2 = embed("Redis cache layer exists")
    mid2 = await db.store_memory(
        content="Redis cache layer exists",
        embedding=emb2,
        project_id=project_id,
        owner_user_id=user_uid,
        importance="high",
        memory_class="memory",
    )
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT source_kind FROM memories WHERE id = $1", mid2,
        )
    check("NULL source_kind when omitted", row["source_kind"] is None)

    # --- Store a rule and a model-inferred for health variety ---
    mid3 = await db.store_memory(
        content="Never delete production data",
        embedding=embed("Never delete production data"),
        project_id=project_id,
        owner_user_id=user_uid,
        importance="critical",
        memory_class="rule",
        source_kind="user-stated",
    )
    mid4 = await db.store_memory(
        content="User seems to prefer dark mode based on terminal config",
        embedding=embed("User seems to prefer dark mode based on terminal config"),
        project_id=project_id,
        owner_user_id=user_uid,
        importance="low",
        memory_class="preference",
        source_kind="model-inferred",
    )

    # --- Add a fact for fact stats ---
    fact_id = await db.add_fact(
        subject="redis",
        predicate="port",
        object="6379",
        project_id=project_id,
        owner_user_id=user_uid,
    )

    # --- Health stats ---
    stats = await db.get_health_stats(user_uid, project_id)

    check("health total_memories >= 4", stats["total_memories"] >= 4)
    check("health by_class.rule >= 1", stats["by_class"]["rule"] >= 1)
    check("health by_class.preference >= 1", stats["by_class"]["preference"] >= 1)
    check("health by_class.memory >= 1", stats["by_class"]["memory"] >= 1)
    check("health by_importance.critical >= 1", stats["by_importance"]["critical"] >= 1)
    check("health by_importance.low >= 1", stats["by_importance"]["low"] >= 1)
    check("health by_source.user-stated >= 2", stats["by_source"]["user-stated"] >= 2)
    check("health by_source.model-inferred >= 1", stats["by_source"]["model-inferred"] >= 1)
    check("health temperature.avg is float", isinstance(stats["temperature"]["avg"], float))
    check("health facts.current >= 1", stats["facts"]["current"] >= 1)

    # --- Never-accessed: mid3 and mid4 were just stored, access_count=0 ---
    check("health never_accessed >= 2", stats["never_accessed"] >= 2)

    # --- Cleanup ---
    async with rls.admin_conn(pool) as conn:
        await conn.execute("DELETE FROM facts WHERE project_id = $1", project_id)
        await conn.execute("DELETE FROM memories WHERE project_id = $1", project_id)
        await conn.execute("DELETE FROM projects WHERE id = $1", project_id)
        await conn.execute("DELETE FROM users WHERE id = $1", user_uid)

    print(f"\n{'='*60}")
    print(f"  Results: {total - failed}/{total} passed, {failed} failed")
    print(f"{'='*60}")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
