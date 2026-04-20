"""
Unit tests for lib/password_policy.py.

Tests run without hitting the real HIBP API by monkeypatching
`_query_hibp_sync` with an in-memory stub. This keeps the suite
hermetic and deterministic — we're testing our logic, not the wire
protocol, and network flakiness should never break a test run.

One optional test at the bottom exercises the real API when env
NNM_TEST_HIBP_LIVE=1 is set.

Usage:
    python tests/test_password_policy.py
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib import password_policy


def run() -> int:
    failed = 0

    def check(label, cond):
        nonlocal failed
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failed += 1

    # -- Short password rejected by length alone, no HIBP call ---------------
    called = {"n": 0}

    def fail_if_called(prefix):
        called["n"] += 1
        return set()

    password_policy._query_hibp_sync = fail_if_called
    err = asyncio.run(password_policy.validate_new_password("short"))
    check("short password rejected with length message", err is not None and "8" in err)
    check("HIBP not called for short password", called["n"] == 0)

    # -- Known-breached password (stub HIBP returning the matching suffix) ---
    def stub_hibp_known_breach(prefix):
        # "password" → SHA-1 = 5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8
        # prefix = "5BAA6", suffix = "1E4C9B93F3F0682250B6CF8331B7EE68FD8"
        if prefix == "5BAA6":
            return {"1E4C9B93F3F0682250B6CF8331B7EE68FD8", "AAAAA"}
        return set()

    password_policy._query_hibp_sync = stub_hibp_known_breach
    err = asyncio.run(password_policy.validate_new_password("password"))
    check("breached password rejected", err is not None)
    check("rejection message mentions breach", err and "breach" in err.lower())

    # -- Same logic but the stub returns a set that does NOT include our
    #    suffix: should ACCEPT -----------------------------------------------
    def stub_hibp_clean(prefix):
        return {"SOMETHINGELSE"}

    password_policy._query_hibp_sync = stub_hibp_clean
    err = asyncio.run(
        password_policy.validate_new_password("ThisIsAGoodPassphrase-2026"),
    )
    check("clean password accepted", err is None)

    # -- Fail-open when HIBP returns None (network failure) ------------------
    def stub_hibp_fail(prefix):
        return None

    password_policy._query_hibp_sync = stub_hibp_fail
    err = asyncio.run(
        password_policy.validate_new_password("another-decent-passphrase"),
    )
    check("HIBP failure: password still accepted (fail-open)", err is None)

    # -- is_pwned returns True / False / None --------------------------------
    password_policy._query_hibp_sync = stub_hibp_known_breach
    check("is_pwned True for known breach",
          asyncio.run(password_policy.is_pwned("password")) is True)

    password_policy._query_hibp_sync = stub_hibp_clean
    check("is_pwned False when suffix not in response",
          asyncio.run(password_policy.is_pwned("password")) is False)

    password_policy._query_hibp_sync = stub_hibp_fail
    check("is_pwned None on query failure",
          asyncio.run(password_policy.is_pwned("password")) is None)

    # -- Empty / None input edge cases ---------------------------------------
    check("is_pwned('') returns None",
          asyncio.run(password_policy.is_pwned("")) is None)

    # -- Optional: live HIBP smoke (opt-in via env) --------------------------
    if os.environ.get("NNM_TEST_HIBP_LIVE") == "1":
        # Restore real sync query
        import importlib
        importlib.reload(password_policy)
        # "password" is the canonical breached password
        live = asyncio.run(password_policy.is_pwned("password"))
        check("live HIBP: 'password' is flagged", live is True)
        live_clean = asyncio.run(
            password_policy.is_pwned(
                "a-very-unlikely-string-" + os.urandom(12).hex(),
            ),
        )
        check("live HIBP: random string is not flagged", live_clean is False)

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
