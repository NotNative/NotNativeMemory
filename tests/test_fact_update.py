"""
Integration tests for fact editing (update_fact in db.py).

Exercises:
- Update subject, predicate, object individually
- Update confidence
- Reject update on superseded facts (valid_to IS NOT NULL)
- Reject update on non-existent or cross-user facts
- No-op when nothing passed

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_fact_update.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID, uuid4

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


async def run() -> int:
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id

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
    user_name = f"fact-update-{run_id}"

    pool = await db.get_pool()
    user = await auth_db.create_user(user_name, "password-1234")
    user_uid = UUID(user["id"])
    set_current_user_id(user_uid)

    project_dir = f"/tmp/fact-update-{run_id}"
    project_id = await db.get_or_create_project(project_dir, owner_user_id=user_uid)

    # Seed a fact
    fact_id = await db.add_fact(
        subject="test-service",
        predicate="port",
        object="8080",
        project_id=project_id,
        owner_user_id=user_uid,
    )

    # --- Update object ---
    ok = await db.update_fact(fact_id, user_uid, object="9090")
    check("update object succeeds", ok is True)

    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow("SELECT object FROM facts WHERE id = $1", fact_id)
    check("object updated in DB", row["object"] == "9090")

    # --- Update subject ---
    ok = await db.update_fact(fact_id, user_uid, subject="new-service")
    check("update subject succeeds", ok is True)

    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow("SELECT subject FROM facts WHERE id = $1", fact_id)
    check("subject updated in DB", row["subject"] == "new-service")

    # --- Update confidence ---
    ok = await db.update_fact(fact_id, user_uid, confidence=0.75)
    check("update confidence succeeds", ok is True)

    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow("SELECT confidence FROM facts WHERE id = $1", fact_id)
    check("confidence updated in DB", abs(row["confidence"] - 0.75) < 0.001)

    # --- No-op returns False ---
    ok = await db.update_fact(fact_id, user_uid)
    check("no-op update returns False", ok is False)

    # --- Cross-user rejected ---
    other = await auth_db.create_user(f"other-{run_id}", "password-1234")
    other_uid = UUID(other["id"])
    ok = await db.update_fact(fact_id, other_uid, object="hacked")
    check("cross-user update rejected", ok is False)

    # Verify unchanged
    async with rls.admin_conn(pool) as conn:
        row = await conn.fetchrow("SELECT object FROM facts WHERE id = $1", fact_id)
    check("object unchanged after cross-user attempt", row["object"] == "9090")

    # --- Superseded fact rejected ---
    # Supersede by adding same subject+predicate with new value
    await db.add_fact(
        subject="new-service",
        predicate="port",
        object="7070",
        project_id=project_id,
        owner_user_id=user_uid,
    )
    # Original fact should now have valid_to set
    ok = await db.update_fact(fact_id, user_uid, object="should-fail")
    check("update on superseded fact rejected", ok is False)

    # --- Unknown fact ID ---
    ok = await db.update_fact(uuid4(), user_uid, object="nope")
    check("update on unknown ID returns False", ok is False)

    # --- Cleanup ---
    async with rls.admin_conn(pool) as conn:
        await conn.execute("DELETE FROM facts WHERE project_id = $1", project_id)
        await conn.execute("DELETE FROM projects WHERE id = $1", project_id)
        await conn.execute("DELETE FROM users WHERE id = $1", user_uid)
        await conn.execute("DELETE FROM users WHERE id = $1", other_uid)

    print(f"\n{'='*60}")
    print(f"  Results: {total - failed}/{total} passed, {failed} failed")
    print(f"{'='*60}")
    return failed


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
