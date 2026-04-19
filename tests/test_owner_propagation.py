"""
End-to-end check that owner_user_id propagates through the write path.

Exercises lib.db.store_memory / add_fact / get_or_create_project
against a live Postgres and verifies the inserted rows carry the
expected owner_user_id.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)
    MEMORY_MODEL_PATH                            (ignored; embedding skipped)

Usage:
    MEMORY_DB_HOST=... python tests/test_owner_propagation.py
"""

import asyncio
import os
import sys
from uuid import UUID

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


async def run() -> int:
    from lib import auth_db, db
    from lib.auth_context import set_current_user_id, current_user_id

    failed = 0

    def check(label, condition):
        nonlocal failed
        if condition:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    # Clean the users table so this test is reproducible.
    pool = await db.get_pool()
    await pool.execute("TRUNCATE users CASCADE")

    # 1. Create a user directly via auth_db.
    user = await auth_db.create_user(
        "owner-test", "password-1234", is_admin=False,
    )
    uid = UUID(user["id"])
    check("created test user", uid is not None)

    # 2. Populate the contextvar as if the middleware had resolved a token.
    set_current_user_id(uid)
    check("contextvar reflects set", current_user_id() == uid)

    # 3. Create a project and verify owner_user_id lands.
    project_id = await db.get_or_create_project(
        "/tmp/owner-test-proj",
        owner_user_id=uid,
    )
    row = await pool.fetchrow(
        "SELECT owner_user_id FROM projects WHERE id = $1",
        project_id,
    )
    check(
        "project.owner_user_id matches",
        row["owner_user_id"] == uid,
    )

    # 4. store_memory with an arbitrary 768-dim zero vector (bypasses
    #    the embedding model so this test stays self-contained).
    zeros = [0.0] * 768
    mem_id = await db.store_memory(
        content="owner-propagation-test memory",
        embedding=zeros,
        project_id=project_id,
        importance="normal",
        owner_user_id=uid,
    )
    row = await pool.fetchrow(
        "SELECT owner_user_id FROM memories WHERE id = $1", mem_id,
    )
    check(
        "memories.owner_user_id matches",
        row is not None and row["owner_user_id"] == uid,
    )

    # 5. add_fact with the same user.
    result = await db.add_fact(
        project_id=project_id,
        subject="owner-propagation",
        predicate="test",
        obj="passed",
        owner_user_id=uid,
    )
    row = await pool.fetchrow(
        "SELECT owner_user_id FROM facts WHERE id = $1",
        UUID(result["id"]),
    )
    check(
        "facts.owner_user_id matches",
        row is not None and row["owner_user_id"] == uid,
    )

    # 6. Write with no owner set (simulates stdio / legacy path).
    set_current_user_id(None)
    project2_id = await db.get_or_create_project(
        "/tmp/owner-test-proj-unowned",
        owner_user_id=None,
    )
    mem2_id = await db.store_memory(
        content="legacy-style unowned memory",
        embedding=zeros,
        project_id=project2_id,
        importance="normal",
        owner_user_id=None,
    )
    row = await pool.fetchrow(
        "SELECT owner_user_id FROM memories WHERE id = $1", mem2_id,
    )
    check(
        "unowned memory has NULL owner",
        row is not None and row["owner_user_id"] is None,
    )

    # Cleanup
    await pool.execute("DELETE FROM memories WHERE id IN ($1, $2)", mem_id, mem2_id)
    await pool.execute("DELETE FROM projects WHERE id IN ($1, $2)", project_id, project2_id)
    await db.close_pool()

    print("---")
    print(f"{6 - failed}/6 passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
