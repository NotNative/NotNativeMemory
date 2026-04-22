"""
Regression test that RLS policies actually prevent cross-user reads
when the caller is a non-superuser role.

The dev DB's `memory` user is typically a superuser, which ALWAYS
bypasses RLS. So this test:

  1. Creates a temporary non-superuser role (_nnm_rls_test_role).
  2. GRANTs it minimal privileges on the user-scoped tables.
  3. `SET ROLE` to that role within the test session (the outer
     session remains the superuser for setup/teardown).
  4. Seeds two users (alice, bob) with one memory each via the
     superuser connection (so setup isn't subject to RLS).
  5. Under the non-superuser role, runs various scenarios:
       - `app.current_user` unset: every table returns 0 rows.
       - SET to alice's id: only alice's rows visible.
       - SET to bob's id: only bob's rows visible.
       - SET to 'admin' sentinel: both users' rows visible.
  6. RESET ROLE, cleans up rows, drops the test role.

Usage:
    python tests/test_rls_enforcement.py

Requires MEMORY_DB_* pointing at a superuser able to CREATE ROLE.
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib.embeddings import EMBEDDING_DIM


TEST_ROLE = "_nnm_rls_test_role"
# Using an ASCII password since we write it via SQL string literal.
TEST_ROLE_PW = "nnm_rls_test_pw"


async def run() -> int:
    from lib import auth_db, db

    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    pool = await db.get_pool()

    # Skip cleanly if caller's role can't CREATE ROLE (e.g. running
    # against a non-superuser dev DB). The rest of the test is moot
    # because we can't even make a role to test against.
    try:
        async with pool.acquire() as setup:
            await setup.execute(
                f"DROP ROLE IF EXISTS {TEST_ROLE}",
            )
            await setup.execute(
                f"CREATE ROLE {TEST_ROLE} LOGIN PASSWORD '{TEST_ROLE_PW}' "
                "NOSUPERUSER NOBYPASSRLS",
            )
            await setup.execute(
                f"GRANT USAGE ON SCHEMA public TO {TEST_ROLE}"
            )
            await setup.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE "
                f"ON memories, facts, projects, auth_tokens, users "
                f"TO {TEST_ROLE}"
            )
    except Exception as exc:
        print(f"  SKIP  cannot CREATE ROLE (need superuser): {exc}")
        return 0

    alice_name = f"rls-alice-{secrets.token_hex(4)}"
    bob_name = f"rls-bob-{secrets.token_hex(4)}"

    try:
        # Seed two users + one memory each, via the superuser connection.
        alice = await auth_db.create_user(alice_name, "password-1234")
        bob = await auth_db.create_user(bob_name, "password-1234")
        alice_uid = UUID(alice["id"])
        bob_uid = UUID(bob["id"])

        # Create a project per user (via raw SQL so the test doesn't
        # depend on get_or_create_project's own RLS semantics).
        alice_project = await pool.fetchval(
            "INSERT INTO projects (directory, name, scope, owner_user_id) "
            "VALUES ($1, $2, 'local', $3) RETURNING id",
            f"/tmp/rls-test-alice-{secrets.token_hex(4)}",
            "rls-test-alice", alice_uid,
        )
        bob_project = await pool.fetchval(
            "INSERT INTO projects (directory, name, scope, owner_user_id) "
            "VALUES ($1, $2, 'local', $3) RETURNING id",
            f"/tmp/rls-test-bob-{secrets.token_hex(4)}",
            "rls-test-bob", bob_uid,
        )

        zeros = "[" + ",".join(["0.0"] * EMBEDDING_DIM) + "]"
        alice_mem = await pool.fetchval(
            "INSERT INTO memories (project_id, content, embedding, owner_user_id) "
            "VALUES ($1, $2, $3::vector, $4) RETURNING id",
            alice_project, "alice-only memory", zeros, alice_uid,
        )
        bob_mem = await pool.fetchval(
            "INSERT INTO memories (project_id, content, embedding, owner_user_id) "
            "VALUES ($1, $2, $3::vector, $4) RETURNING id",
            bob_project, "bob-only memory", zeros, bob_uid,
        )

        # Now SET ROLE and validate RLS.
        async with pool.acquire() as conn:
            await conn.execute(f"SET ROLE {TEST_ROLE}")

            # -- Unset GUC: nothing visible -----------------------------
            await conn.execute("SELECT set_config('app.current_user', '', false)")
            n = await conn.fetchval("SELECT COUNT(*) FROM memories")
            check("unset GUC: memories count = 0", n == 0)
            n = await conn.fetchval("SELECT COUNT(*) FROM projects")
            check("unset GUC: projects count = 0", n == 0)

            # -- Alice's id: only Alice's rows --------------------------
            await conn.execute(
                "SELECT set_config('app.current_user', $1::text, false)",
                str(alice_uid),
            )
            alice_memories = await conn.fetch("SELECT id, content FROM memories")
            alice_ids = {r["id"] for r in alice_memories}
            check("alice sees alice_mem", alice_mem in alice_ids)
            check("alice does NOT see bob_mem", bob_mem not in alice_ids)
            check("alice sees exactly 1 memory", len(alice_memories) == 1)

            alice_projects = await conn.fetch("SELECT id FROM projects")
            alice_pids = {r["id"] for r in alice_projects}
            check("alice sees her project", alice_project in alice_pids)
            check("alice does NOT see bob's project",
                  bob_project not in alice_pids)

            # -- Bob's id: only Bob's rows ------------------------------
            await conn.execute(
                "SELECT set_config('app.current_user', $1::text, false)",
                str(bob_uid),
            )
            bob_memories = await conn.fetch("SELECT id FROM memories")
            bob_ids = {r["id"] for r in bob_memories}
            check("bob sees bob_mem", bob_mem in bob_ids)
            check("bob does NOT see alice_mem", alice_mem not in bob_ids)

            # -- Admin sentinel: both users' rows -----------------------
            await conn.execute(
                "SELECT set_config('app.current_user', 'admin', false)"
            )
            all_memories = await conn.fetch("SELECT id FROM memories")
            all_ids = {r["id"] for r in all_memories}
            check("admin sentinel sees alice_mem", alice_mem in all_ids)
            check("admin sentinel sees bob_mem", bob_mem in all_ids)

            # -- Cross-user WRITE blocked -------------------------------
            # As alice, try to INSERT a memory claiming bob as owner.
            # WITH CHECK should reject it.
            await conn.execute(
                "SELECT set_config('app.current_user', $1::text, false)",
                str(alice_uid),
            )
            try:
                await conn.execute(
                    "INSERT INTO memories (project_id, content, embedding, owner_user_id) "
                    "VALUES ($1, $2, $3::vector, $4)",
                    bob_project, "alice trying to fake a bob memory",
                    zeros, bob_uid,
                )
                check(
                    "cross-user INSERT blocked by WITH CHECK",
                    False,  # should have raised
                )
            except Exception:
                check("cross-user INSERT blocked by WITH CHECK", True)

            await conn.execute("RESET ROLE")

    finally:
        # Clean up: the superuser connection is unaffected by RLS.
        await pool.execute(
            "DELETE FROM users WHERE username = ANY($1)",
            [alice_name, bob_name],
        )
        # The projects + memories cascade with the user delete.
        async with pool.acquire() as conn:
            await conn.execute(
                f"REVOKE SELECT, INSERT, UPDATE, DELETE "
                f"ON memories, facts, projects, auth_tokens, users "
                f"FROM {TEST_ROLE}"
            )
            await conn.execute(
                f"REVOKE USAGE ON SCHEMA public FROM {TEST_ROLE}"
            )
            await conn.execute(f"DROP ROLE IF EXISTS {TEST_ROLE}")

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
