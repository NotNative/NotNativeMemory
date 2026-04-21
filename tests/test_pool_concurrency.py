"""
Concurrency test for lib/db.get_pool().

Before the Phase 2 fix, two coroutines that both saw `_pool is None`
could both proceed through the migration check and both call
asyncpg.create_pool, leaving one pool orphaned. With an asyncio.Lock
protecting the slow path and a double-check inside it, concurrent
first-callers must all converge on the same pool object.

This test fires many concurrent get_pool() calls from a clean state
(pool unset) and asserts they all return the SAME pool instance.

Cross-process serialization (via pg_advisory_lock inside
_run_migrations_on_conn) is not directly exercised here because
spawning a second Python process inside a test is heavier than the
assertion is worth. The in-process lock is the primary guard we can
verify cheaply.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_pool_concurrency.py
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


async def run() -> int:
    from lib import db

    failed = 0

    def check(label, cond):
        nonlocal failed
        if cond:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    # Start clean. A prior test in the same run may have left a pool
    # cached; force a fresh cold start so the lock's slow path is the
    # path under test.
    if db._pool is not None:
        await db.close_pool()
    check("starting state: no pool cached", db._pool is None)

    # Fire 10 concurrent get_pool() callers. All should resolve to the
    # same pool object. Without the lock + double-check, at least one
    # would have been a duplicate pool that leaked.
    results = await asyncio.gather(*[db.get_pool() for _ in range(10)])
    check("all 10 concurrent callers got a non-None pool",
          all(p is not None for p in results))

    first = results[0]
    all_same = all(p is first for p in results)
    check("all 10 concurrent callers got the SAME pool object "
          "(no duplicate pool created)", all_same)

    # Module-level _pool should now point at the same object.
    check("db._pool is the shared object", db._pool is first)

    # A subsequent get_pool() on the fast path returns the same one.
    again = await db.get_pool()
    check("sequential get_pool() after init returns the cached pool",
          again is first)

    # Another burst after the pool exists should never re-enter the
    # slow path (fast path returns immediately).
    burst = await asyncio.gather(*[db.get_pool() for _ in range(20)])
    check("post-init burst: every call returns the cached pool",
          all(p is first for p in burst))

    await db.close_pool()
    check("close_pool() cleared the cache", db._pool is None)

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
