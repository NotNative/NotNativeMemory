"""
Integration tests for the memory classification (class) field.

Exercises:
- Storing with explicit class (rule, preference, memory)
- Storing with no class (NULL / unclassified)
- Updating class via memory_update
- Clearing class (setting to NULL)
- Invalid class values rejected
- Rules exempt from thermal decay (displacement cooling)
- Rules exempt from cap eviction
- Class surfaces in search results
- Class filter in memory_list

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)
    MEMORY_MODEL_PATH                            (loaded; embeds real text)

Usage:
    python tests/test_memory_class.py
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
    import server
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
    user_name = f"class-test-{run_id}"

    pool = await db.get_pool()
    user = await auth_db.create_user(user_name, "password-1234")
    user_uid = UUID(user["id"])
    set_current_user_id(user_uid)

    project_dir = f"/tmp/class-test-{run_id}"
    project_id = await db.get_or_create_project(project_dir, owner_user_id=user_uid)

    # --- Store with explicit class ---

    result = await server.memory_store(
        content="Never use em-dashes in any output.",
        importance="critical",
        memory_class="rule",
        project=project_dir,
    )
    check("store with class=rule succeeds", result.get("stored") is True)
    rule_id = result.get("id")

    result = await server.memory_store(
        content="User prefers terse responses.",
        memory_class="preference",
        project=project_dir,
    )
    check("store with class=preference succeeds", result.get("stored") is True)
    pref_id = result.get("id")

    result = await server.memory_store(
        content="User mentioned SearXNG is on jill.",
        memory_class="memory",
        project=project_dir,
    )
    check("store with class=memory succeeds", result.get("stored") is True)
    mem_id = result.get("id")

    # --- Store without class (unclassified) ---

    result = await server.memory_store(
        content="Unclassified observation about the project.",
        project=project_dir,
    )
    check("store without class succeeds", result.get("stored") is True)
    unclass_id = result.get("id")

    # Verify NULL in DB
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT class FROM memories WHERE id = $1", UUID(unclass_id),
        )
    check("unclassified memory has NULL class", row["class"] is None)

    # Verify rule class persisted
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT class FROM memories WHERE id = $1", UUID(rule_id),
        )
    check("rule class persisted", row["class"] == "rule")

    # --- Invalid class rejected ---

    result = await server.memory_store(
        content="This should fail.",
        memory_class="bogus",
        project=project_dir,
    )
    check("invalid class rejected on store", result.get("stored") is False)

    # --- Update class via memory_update ---

    result = await server.memory_update(
        memory_id=unclass_id, memory_class="preference",
    )
    check("update class to preference succeeds", result.get("updated") is True)

    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT class FROM memories WHERE id = $1", UUID(unclass_id),
        )
    check("class updated to preference in DB", row["class"] == "preference")

    # --- Clear class (set to NULL via "unclassified") ---

    result = await server.memory_update(
        memory_id=unclass_id, memory_class="unclassified",
    )
    check("clear class (set to NULL) succeeds", result.get("updated") is True)

    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT class FROM memories WHERE id = $1", UUID(unclass_id),
        )
    check("class cleared to NULL in DB", row["class"] is None)

    # --- Invalid class rejected on update ---

    result = await server.memory_update(
        memory_id=unclass_id, memory_class="invalid",
    )
    check("invalid class rejected on update", result.get("updated") is False)

    # --- Class surfaces in search results ---

    result = await server.memory_search(
        query="em-dashes output", project=project_dir,
    )
    check("search returns results", len(result.get("results", [])) > 0)
    if result.get("results"):
        hit = result["results"][0]
        check("search result includes class field", "class" in hit)
        check("search result class is 'rule'", hit.get("class") == "rule")

    # --- Class filter in memory_list ---

    result = await server.memory_list(
        project=project_dir, memory_class="rule",
    )
    check("list filter class=rule returns results",
          result.get("count", 0) >= 1)
    for m in result.get("memories", []):
        check(f"list class=rule: item {m['id'][:8]} has class=rule",
              m.get("class") == "rule")

    result = await server.memory_list(
        project=project_dir, memory_class="unclassified",
    )
    check("list filter class=unclassified returns NULL-class only",
          all(m.get("class") is None for m in result.get("memories", [])))

    # --- Dedup merge propagates class into NULL existing row ---

    dedup_content = "Dedup class propagation test content unique-" + run_id
    dedup_vec = embed(dedup_content)
    first_id = await db.store_memory(
        content=dedup_content,
        embedding=dedup_vec,
        project_id=project_id,
        owner_user_id=user_uid,
        tags=["dedup-test"],
        importance="normal",
        memory_class=None,
    )
    # Verify starts unclassified
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT class FROM memories WHERE id = $1", first_id,
        )
    check("dedup target starts with NULL class", row["class"] is None)

    # Store near-identical content with class=preference -- should dedup
    dedup_id = await db.store_memory(
        content=dedup_content,
        embedding=dedup_vec,
        project_id=project_id,
        owner_user_id=user_uid,
        tags=["dedup-test"],
        importance="normal",
        memory_class="preference",
    )
    check("dedup merge returned same id", dedup_id == first_id)

    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT class FROM memories WHERE id = $1", first_id,
        )
    check("dedup merge propagated class to NULL row", row["class"] == "preference")

    # Store again with class=memory -- should NOT overwrite existing class
    await db.store_memory(
        content=dedup_content,
        embedding=dedup_vec,
        project_id=project_id,
        owner_user_id=user_uid,
        tags=["dedup-test"],
        importance="normal",
        memory_class="memory",
    )
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT class FROM memories WHERE id = $1", first_id,
        )
    check("dedup merge preserves existing non-NULL class", row["class"] == "preference")

    # --- Rules exempt from displacement cooling ---

    async with rls.admin_conn(pool) as conn:
        pre_temp = await conn.fetchval(
            "SELECT temperature FROM memories WHERE id = $1", UUID(rule_id),
        )

    # Force cooling by storing many memories
    for i in range(20):
        await db.store_memory(
            content=f"Filler memory {run_id} number {i} for cooling test",
            embedding=embed(f"filler {i}"),
            project_id=project_id,
            owner_user_id=user_uid,
            tags=["filler"],
            importance="low",
        )

    async with rls.admin_conn(pool) as conn:
        post_temp = await conn.fetchval(
            "SELECT temperature FROM memories WHERE id = $1", UUID(rule_id),
        )
    check("rule temperature unchanged after displacement cooling",
          post_temp == pre_temp)

    # --- Rules exempt from cap eviction ---

    # Check rule still exists after filling project
    async with rls.admin_conn(pool) as conn:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM memories WHERE id = $1)", UUID(rule_id),
        )
    check("rule survives cap eviction", exists is True)

    # --- Cleanup ---

    async with rls.admin_conn(pool) as conn:
        await conn.execute(
            "DELETE FROM memories WHERE project_id = $1", project_id,
        )
        await conn.execute(
            "DELETE FROM projects WHERE id = $1", project_id,
        )
        await conn.execute(
            "DELETE FROM users WHERE id = $1", user_uid,
        )

    print(f"\n{'='*60}")
    print(f"  Results: {total - failed}/{total} passed, {failed} failed")
    print(f"{'='*60}")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
