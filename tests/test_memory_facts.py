"""
Integration tests for the temporal fact store (add_fact, query_facts).

Covers the supersession and as_of time-travel contract:

- add_fact invalidates any existing fact with the same
  (project_id, subject, predicate, owner_user_id) by setting its
  valid_to to now(), then inserts the new row.
- query_facts without as_of returns only current facts
  (valid_to IS NULL).
- query_facts with as_of = T returns facts where
  valid_from <= T AND (valid_to IS NULL OR valid_to > T).
  The valid_to bound is strict: a fact with valid_to = T is NOT
  visible at as_of = T.
- Supersession is scoped: different predicates stay valid side by side,
  different projects don't supersede each other, different subjects are
  independent.
- Cross-user isolation: one user's facts cannot supersede another's.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_memory_facts.py
"""

import asyncio
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
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
    alice_name = f"facts-alice-{run_id}"
    bob_name = f"facts-bob-{run_id}"

    pool = await db.get_pool()

    alice_row = await auth_db.create_user(alice_name, "password-1234")
    alice_uid = UUID(alice_row["id"])
    bob_row = await auth_db.create_user(bob_name, "password-1234")
    bob_uid = UUID(bob_row["id"])

    set_current_user_id(alice_uid)

    try:
        # ================================================================
        # Scenario 1: add + query without as_of
        # ================================================================
        proj_a = await db.get_or_create_project(
            f"/tmp/facts-A-{run_id}", owner_user_id=alice_uid,
        )

        result = await db.add_fact(
            project_id=proj_a, subject="python-version",
            predicate="is", obj="3.12",
            owner_user_id=alice_uid,
        )
        check("add_fact returned a new id",
              result.get("id") is not None)
        check("add_fact initial add: no prior superseded",
              result.get("superseded") == 0)

        current = await db.query_facts(
            owner_user_id=alice_uid, subject="python-version",
            project_id=proj_a,
        )
        check("query without as_of returns the single current fact",
              len(current) == 1)
        check("returned fact has the expected object",
              current and current[0]["object"] == "3.12")
        check("current fact has valid_to = None",
              current and current[0]["valid_to"] is None)

        # ================================================================
        # Scenario 2: supersession (same subject + predicate -> old
        # gets valid_to, new is current)
        # ================================================================
        original_valid_from_iso = current[0]["valid_from"]

        # Small sleep to guarantee the second fact's valid_from is
        # strictly later than the first's valid_to for safe boundary
        # tests below.
        await asyncio.sleep(0.05)

        result2 = await db.add_fact(
            project_id=proj_a, subject="python-version",
            predicate="is", obj="3.13",
            owner_user_id=alice_uid,
        )
        check("supersession: add_fact reports prior superseded",
              result2.get("superseded") == 1)

        current2 = await db.query_facts(
            owner_user_id=alice_uid, subject="python-version",
            project_id=proj_a,
        )
        check("current-only query returns exactly one row after supersession",
              len(current2) == 1)
        check("current is now 3.13, not 3.12",
              current2 and current2[0]["object"] == "3.13")

        # ================================================================
        # Scenario 3: as_of time-travel (return the historical value)
        # ================================================================
        # The old fact's valid_to is ~equal to the new fact's valid_from.
        # Pick an as_of that lies strictly between original_valid_from
        # and the new valid_from.
        old_valid_from = datetime.fromisoformat(original_valid_from_iso)
        new_valid_from = datetime.fromisoformat(current2[0]["valid_from"])
        mid = old_valid_from + (new_valid_from - old_valid_from) / 2

        historical = await db.query_facts(
            owner_user_id=alice_uid, subject="python-version",
            project_id=proj_a, as_of=mid,
        )
        check("as_of between old valid_from and supersession: returns "
              "historical value (3.12)",
              len(historical) == 1 and historical[0]["object"] == "3.12")

        # as_of before the first fact existed: no results
        pre_existence = old_valid_from - timedelta(hours=1)
        nothing = await db.query_facts(
            owner_user_id=alice_uid, subject="python-version",
            project_id=proj_a, as_of=pre_existence,
        )
        check("as_of before any fact existed returns empty",
              nothing == [])

        # as_of well after supersession: returns current value
        future = new_valid_from + timedelta(hours=1)
        after = await db.query_facts(
            owner_user_id=alice_uid, subject="python-version",
            project_id=proj_a, as_of=future,
        )
        check("as_of after supersession returns current value",
              len(after) == 1 and after[0]["object"] == "3.13")

        # ================================================================
        # Scenario 4: valid_to boundary is strict (fact with valid_to=T
        # is NOT visible at as_of=T)
        # ================================================================
        async with rls.admin_conn(pool) as conn:
            superseded_row = await conn.fetchrow(
                """SELECT valid_to FROM facts
                   WHERE project_id = $1 AND subject = $2
                     AND predicate = $3 AND valid_to IS NOT NULL
                   ORDER BY valid_to DESC LIMIT 1""",
                proj_a, "python-version", "is",
            )
        check("superseded fact has a non-null valid_to",
              superseded_row and superseded_row["valid_to"] is not None)

        boundary = superseded_row["valid_to"]
        at_boundary = await db.query_facts(
            owner_user_id=alice_uid, subject="python-version",
            project_id=proj_a, as_of=boundary,
        )
        # The superseded fact at its valid_to is excluded (strict >).
        # The new fact may or may not be visible depending on whether
        # new.valid_from <= boundary. Since valid_to of the old ~=
        # valid_from of the new in the same transaction, the new fact
        # should be visible at the boundary instant.
        old_visible = any(f["object"] == "3.12" for f in at_boundary)
        check("at as_of = superseded fact's valid_to: old fact excluded "
              "(strict >)", not old_visible)

        # ================================================================
        # Scenario 5: different predicate -> no supersession
        # ================================================================
        await db.add_fact(
            project_id=proj_a, subject="python-version",
            predicate="released-in", obj="2025",
            owner_user_id=alice_uid,
        )
        both = await db.query_facts(
            owner_user_id=alice_uid, subject="python-version",
            project_id=proj_a,
        )
        check("different predicate: both current facts coexist",
              len(both) == 2)
        preds = {f["predicate"] for f in both}
        check("current facts include both 'is' and 'released-in'",
              preds == {"is", "released-in"})

        # ================================================================
        # Scenario 6: cross-project isolation
        # ================================================================
        proj_b = await db.get_or_create_project(
            f"/tmp/facts-B-{run_id}", owner_user_id=alice_uid,
        )
        await db.add_fact(
            project_id=proj_b, subject="python-version",
            predicate="is", obj="3.11",
            owner_user_id=alice_uid,
        )
        proj_a_facts = await db.query_facts(
            owner_user_id=alice_uid, subject="python-version",
            project_id=proj_a,
        )
        is_in_a = [f["object"] for f in proj_a_facts
                   if f["predicate"] == "is"]
        check("project A's 'is' fact is still 3.13 (project B add did not "
              "supersede)", is_in_a == ["3.13"])

        # ================================================================
        # Scenario 7: different subject is independent
        # ================================================================
        await db.add_fact(
            project_id=proj_a, subject="node-version",
            predicate="is", obj="22",
            owner_user_id=alice_uid,
        )
        py_still = await db.query_facts(
            owner_user_id=alice_uid, subject="python-version",
            project_id=proj_a,
        )
        check("different subject doesn't touch python-version facts",
              any(f["object"] == "3.13" for f in py_still))

        # ================================================================
        # Scenario 8: cross-user isolation (bob's fact cannot supersede
        # alice's)
        # ================================================================
        set_current_user_id(bob_uid)
        proj_bob = await db.get_or_create_project(
            f"/tmp/facts-bob-{run_id}", owner_user_id=bob_uid,
        )
        await db.add_fact(
            project_id=proj_bob, subject="python-version",
            predicate="is", obj="2.7",
            owner_user_id=bob_uid,
        )
        # Alice's fact should still be 3.13
        set_current_user_id(alice_uid)
        alice_still = await db.query_facts(
            owner_user_id=alice_uid, subject="python-version",
            project_id=proj_a,
        )
        alice_is = [f["object"] for f in alice_still
                    if f["predicate"] == "is"]
        check("bob's add on same subject+predicate did not supersede "
              "alice's fact", alice_is == ["3.13"])

        # And bob should only see his own fact
        set_current_user_id(bob_uid)
        bob_facts = await db.query_facts(
            owner_user_id=bob_uid, subject="python-version",
            project_id=proj_bob,
        )
        bob_is = [f["object"] for f in bob_facts]
        check("bob's query returns only bob's fact", bob_is == ["2.7"])

    finally:
        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                "DELETE FROM facts WHERE owner_user_id = ANY($1)",
                [alice_uid, bob_uid],
            )
            await conn.execute(
                "DELETE FROM memories WHERE owner_user_id = ANY($1)",
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
