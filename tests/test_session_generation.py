"""
End-to-end check that session revocation via token_generation works.

Exercises lib.auth_db.create_token / resolve_token / bump_token_generation
against a live Postgres and verifies:
  - A fresh token snapshots the user's current generation at mint.
  - resolve_token returns the owner while generations match.
  - Bumping the user's generation makes every outstanding token stale.
  - A token minted after the bump carries the new generation and
    resolves until the next bump.
  - bump_token_generation on a non-existent user raises ValueError.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD   (live pgvector)

Usage:
    python tests/test_session_generation.py

Creates a uniquely-named test user and tears it down in a finally block
so a shared dev DB isn't polluted.
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

    failed = 0

    def check(label, condition):
        nonlocal failed
        if condition:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    pool = await db.get_pool()
    uname = f"gen-test-{secrets.token_hex(4)}"

    try:
        user = await auth_db.create_user(uname, "password-1234")
        uid = UUID(user["id"])

        # Mint-and-resolve at generation 0.
        minted = await auth_db.create_token(uid, label="gen-test")
        raw = minted["token"]
        tid = UUID(minted["id"])

        resolved = await auth_db.resolve_token(raw)
        check(
            "token resolves at generation 0",
            resolved is not None and resolved["user_id"] == uid,
        )

        # auth_tokens is RLS'd; use admin_conn for direct verification
        # SELECTs that span user context.
        async with rls.admin_conn(pool) as conn:
            row = await conn.fetchrow(
                "SELECT issued_generation FROM auth_tokens WHERE id = $1", tid,
            )
        check(
            "token.issued_generation snapshots 0 at mint",
            row["issued_generation"] == 0,
        )

        # Bump invalidates.
        new_gen = await auth_db.bump_token_generation(uid)
        check("bump advances generation to 1", new_gen == 1)
        check(
            "old token rejects after bump",
            await auth_db.resolve_token(raw) is None,
        )

        # Fresh mint picks up the new generation.
        fresh = await auth_db.create_token(uid, label="post-bump")
        check(
            "fresh token resolves at generation 1",
            (await auth_db.resolve_token(fresh["token"])) is not None,
        )

        async with rls.admin_conn(pool) as conn:
            row2 = await conn.fetchrow(
                "SELECT issued_generation FROM auth_tokens WHERE id = $1",
                UUID(fresh["id"]),
            )
        check(
            "fresh token.issued_generation = 1",
            row2["issued_generation"] == 1,
        )

        # Second bump catches everything.
        await auth_db.bump_token_generation(uid)
        check(
            "original token still stale after second bump",
            await auth_db.resolve_token(raw) is None,
        )
        check(
            "previously-fresh token stale after second bump",
            await auth_db.resolve_token(fresh["token"]) is None,
        )

        # Missing user surfaces as ValueError so callers notice.
        try:
            await auth_db.bump_token_generation(
                UUID("00000000-0000-0000-0000-000000000000"),
            )
            check("bump on missing user raises ValueError", False)
        except ValueError:
            check("bump on missing user raises ValueError", True)

    finally:
        await pool.execute("DELETE FROM users WHERE username = $1", uname)

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
