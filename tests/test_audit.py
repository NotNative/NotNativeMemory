"""
Integration smoke for lib/audit.py.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD   (live pgvector)

Creates a uniquely-named user, writes a few audit events, reads them
back from the DB, and cleans up after itself. Safe to run against
the shared dev database — tears down its rows in a finally block.

Usage:
    python tests/test_audit.py
"""

import asyncio
import json
import os
import secrets
import sys
from uuid import UUID, uuid4

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


async def run() -> int:
    from lib import audit, auth_db, db

    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    pool = await db.get_pool()
    uname = f"audit-test-{secrets.token_hex(4)}"

    try:
        user = await auth_db.create_user(uname, "password-1234")
        uid = UUID(user["id"])

        # login.success with full detail
        fake_token_id = uuid4()
        await audit.log_event(
            "login.success",
            actor_user_id=uid,
            target_id=fake_token_id,
            detail={"ip": "127.0.0.1", "ua": "pytest/1.0", "path": "/login"},
        )

        row = await pool.fetchrow(
            """
            SELECT event_type, actor_user_id, target_id, detail::text AS detail_txt
            FROM audit_events
            WHERE actor_user_id = $1
            ORDER BY at DESC LIMIT 1
            """,
            uid,
        )
        check("login.success row landed", row is not None)
        check("event_type is login.success", row["event_type"] == "login.success")
        check("actor_user_id matches", row["actor_user_id"] == uid)
        check("target_id matches", row["target_id"] == fake_token_id)
        detail = json.loads(row["detail_txt"])
        check("detail.ip round-trips", detail["ip"] == "127.0.0.1")
        check("detail.ua round-trips", detail["ua"] == "pytest/1.0")

        # NULL actor path
        await audit.log_event(
            "login.fail",
            actor_user_id=None,
            detail={"ip": "1.2.3.4", "username_tried": uname + "-bogus"},
        )
        row2 = await pool.fetchrow(
            """
            SELECT actor_user_id FROM audit_events
            WHERE event_type='login.fail'
              AND detail->>'username_tried' = $1
            ORDER BY at DESC LIMIT 1
            """,
            uname + "-bogus",
        )
        check("login.fail row landed", row2 is not None)
        check("login.fail actor_user_id is NULL", row2["actor_user_id"] is None)

        # Best-effort semantics: unserializable detail does not raise.
        before = (await pool.fetchrow(
            "SELECT COUNT(*)::int AS n FROM audit_events"
        ))["n"]
        await audit.log_event("weird", detail={"cant-serialize": object()})
        after = (await pool.fetchrow(
            "SELECT COUNT(*)::int AS n FROM audit_events"
        ))["n"]
        check(
            "log_event swallowed unserializable detail (no raise)",
            after == before,  # insert failed silently, no row added, no exception
        )

    finally:
        # Clean up: delete events we wrote, then the user.
        await pool.execute(
            "DELETE FROM audit_events WHERE actor_user_id = $1", uid,
        )
        await pool.execute(
            "DELETE FROM audit_events WHERE detail->>'username_tried' = $1",
            uname + "-bogus",
        )
        await pool.execute("DELETE FROM users WHERE username = $1", uname)

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
