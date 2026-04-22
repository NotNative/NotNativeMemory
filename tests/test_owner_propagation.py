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

from lib.embeddings import EMBEDDING_DIM


async def run() -> int:
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id, current_user_id

    failed = 0

    def check(label, condition):
        nonlocal failed
        if condition:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    # Use a uniquely-named test user so running this against a live
    # DB doesn't collide with (or destroy) real data. The previous
    # version of this test called TRUNCATE users CASCADE which
    # deleted every user's memories, projects, tokens, and facts.
    # Never truncate a shared table from a test.
    import secrets
    test_username = f"owner-test-{secrets.token_hex(4)}"
    test_project_dir = f"/tmp/owner-test-{secrets.token_hex(4)}"

    pool = await db.get_pool()

    # 1. Create a user directly via auth_db. Phase 7 dropped the
    #    is_admin parameter — every user is a peer now.
    user = await auth_db.create_user(test_username, "password-1234")
    uid = UUID(user["id"])
    check("created test user", uid is not None)

    # 2. Populate the contextvar as if the middleware had resolved a token.
    set_current_user_id(uid)
    check("contextvar reflects set", current_user_id() == uid)

    # 3. Create a project and verify owner_user_id lands.
    #
    # Verification SELECTs use rls.admin_conn because under FORCE RLS
    # the pool connects as a non-superuser whose `app.current_user`
    # is unset outside app_conn blocks — so a bare pool.fetchrow sees
    # zero rows. admin_conn sets the sentinel bypass so we can inspect
    # any user's data for test verification. This is test code, not
    # production; the bypass is fine here.
    project_id = await db.get_or_create_project(
        test_project_dir,
        owner_user_id=uid,
    )
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT owner_user_id FROM projects WHERE id = $1",
            project_id,
        )
    check(
        "project.owner_user_id matches",
        row is not None and row["owner_user_id"] == uid,
    )

    # 4. store_memory with an arbitrary zero vector (bypasses the
    #    embedding model so this test stays self-contained).
    zeros = [0.0] * EMBEDDING_DIM
    mem_id = await db.store_memory(
        content="owner-propagation-test memory",
        embedding=zeros,
        project_id=project_id,
        owner_user_id=uid,
        importance="normal",
    )
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
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
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow(
            "SELECT owner_user_id FROM facts WHERE id = $1",
            UUID(result["id"]),
        )
    check(
        "facts.owner_user_id matches",
        row is not None and row["owner_user_id"] == uid,
    )

    # 6. Phase 7 made owner_user_id mandatory — verify the helper
    #    raises instead of silently creating an unowned row.
    raised = False
    try:
        await db.get_or_create_project(
            f"{test_project_dir}-unowned",
            owner_user_id=None,
        )
    except ValueError:
        raised = True
    check("get_or_create_project rejects owner_user_id=None", raised)

    # Cleanup: deleting the test user cascades to their rows
    # (tokens, projects, memories, facts) via the ON DELETE CASCADE
    # FKs on users. Belt-and-suspenders: explicitly wipe the ones
    # we know about in case a future schema change weakens that
    # cascade. Admin conn so the DELETEs aren't filtered by RLS.
    async with rls.admin_conn(pool) as conn:
        await conn.execute("DELETE FROM memories WHERE id = $1", mem_id)
        await conn.execute("DELETE FROM projects WHERE id = $1", project_id)
    await pool.execute("DELETE FROM users WHERE id = $1", uid)
    await db.close_pool()

    print("---")
    print(f"{6 - failed}/6 passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
