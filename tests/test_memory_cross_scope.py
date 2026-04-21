"""
Integration tests for cross-scope project resolution.

Exercises lib/db.get_visible_project_ids, which drives which projects'
memories participate in a search initiated from a given primary
project.

Contract (from the function docstring):
- Searching from a local-scope project includes: self, every global
  project owned by the same user, and every domain project whose name
  appears in the local project's domains[] array.
- Searching from a global or domain project returns only that project.
- Owner filter is always applied: user A cannot pull user B's globals
  even if both users happen to have a project called '_global'.
- A declared domain that does not exist is silently skipped.
- An unconfigured local project (domains=[]) pulls only self + globals.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_memory_cross_scope.py
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

    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    run_id = secrets.token_hex(4)
    alice_name = f"scope-alice-{run_id}"
    bob_name = f"scope-bob-{run_id}"

    pool = await db.get_pool()

    alice = await auth_db.create_user(alice_name, "password-1234")
    alice_uid = UUID(alice["id"])
    bob = await auth_db.create_user(bob_name, "password-1234")
    bob_uid = UUID(bob["id"])

    set_current_user_id(alice_uid)

    try:
        # Alice's scope set.
        alice_local = await db.get_or_create_project(
            f"/tmp/scope-alice-local-{run_id}", owner_user_id=alice_uid,
        )
        alice_global = await db.get_or_create_project(
            "_global", owner_user_id=alice_uid,
        )
        alice_python = await db.get_or_create_project(
            "_domain_python", owner_user_id=alice_uid,
        )
        alice_docker = await db.get_or_create_project(
            "_domain_docker", owner_user_id=alice_uid,
        )
        alice_rust = await db.get_or_create_project(
            "_domain_rust", owner_user_id=alice_uid,
        )

        # ================================================================
        # Scenario 1: local with no declared domains -> self + globals
        # ================================================================
        # By default domains is empty, so the local project pulls only
        # itself and every global owned by alice.
        ids = await db.get_visible_project_ids(alice_local, alice_uid)
        id_set = set(ids)
        check("local (no domains): includes self",
              alice_local in id_set)
        check("local (no domains): includes alice's global",
              alice_global in id_set)
        check("local (no domains): does NOT pull domain memories",
              alice_python not in id_set
              and alice_docker not in id_set
              and alice_rust not in id_set)

        # ================================================================
        # Scenario 2: local with declared domains -> self + globals
        # + matching domains only
        # ================================================================
        await db.set_project_domains(
            alice_local, alice_uid, ["python", "docker"],
        )
        ids = await db.get_visible_project_ids(alice_local, alice_uid)
        id_set = set(ids)
        check("local (python,docker): includes self", alice_local in id_set)
        check("local (python,docker): includes alice's global",
              alice_global in id_set)
        check("local (python,docker): includes alice's _domain_python",
              alice_python in id_set)
        check("local (python,docker): includes alice's _domain_docker",
              alice_docker in id_set)
        check("local (python,docker): excludes alice's _domain_rust "
              "(not declared)", alice_rust not in id_set)

        # ================================================================
        # Scenario 3: non-existent declared domain is silently skipped
        # ================================================================
        await db.set_project_domains(
            alice_local, alice_uid, ["python", "doesnotexist"],
        )
        ids = await db.get_visible_project_ids(alice_local, alice_uid)
        id_set = set(ids)
        check("missing domain: no error, existing ones still included",
              alice_python in id_set)
        check("missing domain: self + global still included",
              alice_local in id_set and alice_global in id_set)

        # ================================================================
        # Scenario 4: searching from a global project returns only it
        # ================================================================
        ids_from_global = await db.get_visible_project_ids(
            alice_global, alice_uid,
        )
        check("from global: returns only the global project itself",
              set(ids_from_global) == {alice_global})

        # ================================================================
        # Scenario 5: searching from a domain project returns only it
        # ================================================================
        ids_from_domain = await db.get_visible_project_ids(
            alice_python, alice_uid,
        )
        check("from domain: returns only the domain project itself",
              set(ids_from_domain) == {alice_python})

        # ================================================================
        # Scenario 6: cross-user isolation. Bob has his own
        # _global and _domain_python. Alice must not see them.
        # ================================================================
        set_current_user_id(bob_uid)
        bob_global = await db.get_or_create_project(
            "_global", owner_user_id=bob_uid,
        )
        bob_python = await db.get_or_create_project(
            "_domain_python", owner_user_id=bob_uid,
        )

        set_current_user_id(alice_uid)
        # Refresh alice's local to declare python.
        await db.set_project_domains(
            alice_local, alice_uid, ["python"],
        )
        ids = await db.get_visible_project_ids(alice_local, alice_uid)
        id_set = set(ids)
        check("cross-user: alice's search does NOT see bob's global",
              bob_global not in id_set)
        check("cross-user: alice's search does NOT see bob's _domain_python "
              "even though alice declared python",
              bob_python not in id_set)
        check("cross-user: alice still sees her own _domain_python",
              alice_python in id_set)

        # And the reverse: bob's search only sees bob's projects.
        set_current_user_id(bob_uid)
        bob_local = await db.get_or_create_project(
            f"/tmp/scope-bob-local-{run_id}", owner_user_id=bob_uid,
        )
        await db.set_project_domains(
            bob_local, bob_uid, ["python"],
        )
        bob_ids = await db.get_visible_project_ids(bob_local, bob_uid)
        bob_set = set(bob_ids)
        check("cross-user: bob's search includes his own local, global, "
              "and domain",
              bob_local in bob_set
              and bob_global in bob_set
              and bob_python in bob_set)
        check("cross-user: bob's search excludes alice's local",
              alice_local not in bob_set)
        check("cross-user: bob's search excludes alice's global",
              alice_global not in bob_set)
        check("cross-user: bob's search excludes alice's _domain_python",
              alice_python not in bob_set)

        # ================================================================
        # Scenario 7: unknown primary id -> returns [primary_id]
        # (graceful fallback per the function's contract)
        # ================================================================
        from uuid import uuid4
        fake_id = uuid4()
        ids_fake = await db.get_visible_project_ids(fake_id, alice_uid)
        check("unknown primary: returns the id alone (no expansion)",
              ids_fake == [fake_id])

    finally:
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                "DELETE FROM memories WHERE owner_user_id = ANY($1)",
                [alice_uid, bob_uid],
            )
            await conn.execute(
                "DELETE FROM facts WHERE owner_user_id = ANY($1)",
                [alice_uid, bob_uid],
            )
            await conn.execute(
                "DELETE FROM projects WHERE owner_user_id = ANY($1)",
                [alice_uid, bob_uid],
            )
        await pool.execute(
            "DELETE FROM users WHERE id = ANY($1)",
            [alice_uid, bob_uid],
        )
        await db.close_pool()

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
