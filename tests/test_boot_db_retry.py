"""
Tests for server._acquire_pool_with_retry.

Verifies the boot-time retry helper that prevents container restart
loops when the app starts before Postgres has finished initializing.

Pure unit tests: no real DB. We pass a fake get_pool callable that
raises N times then returns a sentinel.

Covers:
  - happy path: succeeds on first call, no sleeping
  - retries CannotConnectNowError until success
  - retries ConnectionRefusedError until success
  - fails fast on non-transient errors (InvalidPasswordError)
  - raises if budget exhausted

Usage:
    python tests/test_boot_db_retry.py
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


async def run() -> int:
    import asyncpg
    import server

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

    SENTINEL = object()

    # 1. Happy path: succeeds on first call.
    calls = {"n": 0}

    async def good():
        calls["n"] += 1
        return SENTINEL

    result = await server._acquire_pool_with_retry(good, budget_secs=5.0)
    check("returns the pool on first success", result is SENTINEL)
    check("called exactly once on success", calls["n"] == 1)

    # 2. CannotConnectNowError twice, then succeed.
    calls = {"n": 0}

    async def starting_up_then_ok():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise asyncpg.exceptions.CannotConnectNowError(
                "the database system is starting up"
            )
        return SENTINEL

    result = await server._acquire_pool_with_retry(
        starting_up_then_ok, budget_secs=10.0,
    )
    check("retries CannotConnectNowError until success",
          result is SENTINEL)
    check("called exactly 3 times (2 fails + 1 success)",
          calls["n"] == 3)

    # 3. ConnectionRefusedError once, then succeed.
    calls = {"n": 0}

    async def refused_then_ok():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionRefusedError("nope")
        return SENTINEL

    result = await server._acquire_pool_with_retry(
        refused_then_ok, budget_secs=10.0,
    )
    check("retries ConnectionRefusedError until success",
          result is SENTINEL)
    check("called twice (1 fail + 1 success)", calls["n"] == 2)

    # 4. Non-transient error fails fast.
    calls = {"n": 0}

    class FakeAuthError(Exception):
        pass

    async def bad_creds():
        calls["n"] += 1
        raise FakeAuthError("invalid password")

    raised = None
    try:
        await server._acquire_pool_with_retry(bad_creds, budget_secs=10.0)
    except FakeAuthError as exc:
        raised = exc
    check("non-transient error is not swallowed",
          isinstance(raised, FakeAuthError))
    check("non-transient error fails on first attempt",
          calls["n"] == 1)

    # 5. Budget exhaustion: a fake that always raises must eventually
    #    raise the last retryable exception. Use a tiny budget so the
    #    test stays fast.
    calls = {"n": 0}

    async def always_starting_up():
        calls["n"] += 1
        raise asyncpg.exceptions.CannotConnectNowError("still booting")

    raised = None
    try:
        await server._acquire_pool_with_retry(
            always_starting_up, budget_secs=0.5,
        )
    except asyncpg.exceptions.CannotConnectNowError as exc:
        raised = exc
    check("budget exhaustion raises the retryable error",
          isinstance(raised, asyncpg.exceptions.CannotConnectNowError))
    check("budget exhaustion attempted more than once",
          calls["n"] >= 2)

    print("---")
    print(f"{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
