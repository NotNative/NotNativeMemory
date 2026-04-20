"""
Unit tests for lib/rate_limit.py. No DB, no HTTP — exercises the
bucket logic directly by manipulating time.monotonic() via the
module's _now() indirection.

Usage:
    python tests/test_rate_limit.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib import rate_limit


def run() -> int:
    failed = 0

    def check(label, condition):
        nonlocal failed
        if condition:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    # Patch _now so we can move time forward deterministically.
    clock = {"t": 1_000.0}

    def fake_now() -> float:
        return clock["t"]

    rate_limit._now = fake_now  # monkeypatch — test-only

    def reset_all():
        rate_limit._login_ip._entries.clear()
        rate_limit._login_user._entries.clear()
        rate_limit._register_ip._entries.clear()

    # -- First login attempt is allowed ---------------------------------------
    reset_all()
    clock["t"] = 1000.0
    allowed, wait = rate_limit.check_login("1.2.3.4", "alice")
    check("first login allowed", allowed and wait == 0.0)

    # -- After one failure: 1s backoff (2**0 = 1) -----------------------------
    rate_limit.record_login_failure("1.2.3.4", "alice")
    allowed, wait = rate_limit.check_login("1.2.3.4", "alice")
    check("blocked immediately after first failure", not allowed)
    check("first-failure backoff is ~1s", 0.5 < wait <= 1.0)

    # -- Time passes by 1.1s -> allowed again ---------------------------------
    clock["t"] += 1.1
    allowed, _ = rate_limit.check_login("1.2.3.4", "alice")
    check("allowed after first-failure window elapses", allowed)

    # -- Two failures in a row -> 2s backoff (2**1 = 2) -----------------------
    rate_limit.record_login_failure("1.2.3.4", "alice")  # 2nd consecutive
    allowed, wait = rate_limit.check_login("1.2.3.4", "alice")
    check("blocked after second failure", not allowed)
    check("second-failure backoff is ~2s", 1.5 < wait <= 2.0)

    # -- clear_login resets both IP and user buckets --------------------------
    rate_limit.clear_login("1.2.3.4", "alice")
    allowed, wait = rate_limit.check_login("1.2.3.4", "alice")
    check("clear_login resets the counter", allowed and wait == 0.0)

    # -- Per-username isolation: failing bob doesn't block alice from a new IP
    reset_all()
    clock["t"] = 2000.0
    rate_limit.record_login_failure("5.6.7.8", "bob")
    allowed, _ = rate_limit.check_login("9.9.9.9", "alice")
    check("failing bob@5.6.7.8 does not block alice@9.9.9.9", allowed)

    # -- Per-IP isolation: bob hammering from one IP is blocked there,
    #    but alice from a different IP still works -------------------------
    allowed_bob, _ = rate_limit.check_login("5.6.7.8", "bob")
    check("bob from 5.6.7.8 is blocked (own IP + own user)", not allowed_bob)

    # -- Username hit from different IP is STILL blocked (username bucket) ----
    allowed_bob2, _ = rate_limit.check_login("9.9.9.9", "bob")
    check(
        "bob from a different IP is still blocked (username bucket catches)",
        not allowed_bob2,
    )

    # -- Exponential ceiling: many failures cap at MAX_BACKOFF_SECS ----------
    reset_all()
    clock["t"] = 3000.0
    for _ in range(20):
        rate_limit.record_login_failure("1.1.1.1", "cap")
    _, wait = rate_limit.check_login("1.1.1.1", "cap")
    check(
        "backoff caps at MAX_BACKOFF_SECS",
        wait <= rate_limit.MAX_BACKOFF_SECS,
    )

    # -- Stale entries expire after TTL and allow fresh attempts --------------
    reset_all()
    clock["t"] = 4000.0
    rate_limit.record_login_failure("2.2.2.2", "dave")
    clock["t"] += rate_limit.ENTRY_TTL_SECS + 1
    allowed, wait = rate_limit.check_login("2.2.2.2", "dave")
    check("stale entry forgotten after TTL", allowed and wait == 0.0)

    # -- Registration: each attempt advances the backoff; no clear path ------
    reset_all()
    clock["t"] = 5000.0
    allowed, _ = rate_limit.check_register("3.3.3.3")
    check("first register attempt allowed", allowed)
    rate_limit.record_register_attempt("3.3.3.3")
    allowed, wait = rate_limit.check_register("3.3.3.3")
    check("blocked immediately after first register record", not allowed)
    check("first-register backoff is ~1s", 0.5 < wait <= 1.0)

    # -- Username key is lowercased and length-capped ------------------------
    reset_all()
    clock["t"] = 6000.0
    rate_limit.record_login_failure("7.7.7.7", "AlIcE")
    allowed, _ = rate_limit.check_login("7.7.7.7", "alice")
    check("username key is case-insensitive", not allowed)

    long_name = "z" * 500
    rate_limit.record_login_failure("8.8.8.8", long_name)
    same_prefix = "z" * 64  # truncated key
    allowed, _ = rate_limit.check_login("1.1.1.2", same_prefix)
    check("username key is truncated to 64 chars", not allowed)

    # -- client_ip helper --------------------------------------------------
    class FakeReq:
        pass

    r = FakeReq()
    r.client = None
    check("client_ip handles missing client", rate_limit.client_ip(r) == "unknown")

    class FakeClient:
        host = "10.0.0.1"
    r.client = FakeClient()
    check("client_ip returns host", rate_limit.client_ip(r) == "10.0.0.1")

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
