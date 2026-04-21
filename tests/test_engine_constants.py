"""
Unit tests for the memory engine's tunable constants in lib/db.py.

These are not behavior tests (those are integration-level against a
live Postgres). They are spec tests that lock in the documented values
and their cross-invariants, so an accidental change to one constant
cannot silently shift engine semantics without a test failure.

The behaviors these constants govern:
- TEMP_INITIAL / TEMP_MAX / REHEAT_DELTA: thermal math for new stores
  and reheating on search/merge.
- DISPLACEMENT_COOL_DELTA / PRESSURE_COOL_DELTA / PRESSURE_THRESHOLD:
  how aggressively stale memories cool relative to project fullness.
- _COOL_RATE: per-importance cool multipliers. Critical must be 0.
- _IMPORTANCE_WEIGHT: per-importance search ranking bonus. Ordering
  must be monotonic across the four tiers.
- PROJECT_MEMORY_CAP / DEDUP_SIMILARITY_THRESHOLD: hard limits.

Usage:
    python tests/test_engine_constants.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from lib import db


def run() -> int:
    failed = 0

    def check(label, cond):
        nonlocal failed
        print(f"  {'PASS' if cond else 'FAIL'}  {label}")
        if not cond:
            failed += 1

    # -- Documented values ---------------------------------------------------
    check("TEMP_INITIAL == 70.0", db.TEMP_INITIAL == 70.0)
    check("TEMP_MAX == 95.0", db.TEMP_MAX == 95.0)
    check("REHEAT_DELTA == 10.0", db.REHEAT_DELTA == 10.0)
    check("DISPLACEMENT_COOL_DELTA == 0.5", db.DISPLACEMENT_COOL_DELTA == 0.5)
    check("PRESSURE_COOL_DELTA == 0.5", db.PRESSURE_COOL_DELTA == 0.5)
    check("PRESSURE_THRESHOLD == 0.8", db.PRESSURE_THRESHOLD == 0.8)
    check("PROJECT_MEMORY_CAP == 500", db.PROJECT_MEMORY_CAP == 500)
    check("DEDUP_SIMILARITY_THRESHOLD == 0.92",
          db.DEDUP_SIMILARITY_THRESHOLD == 0.92)
    check("_MIN_STORES_BETWEEN_COOL == 3", db._MIN_STORES_BETWEEN_COOL == 3)

    # -- Thermal invariants --------------------------------------------------
    check("TEMP_INITIAL < TEMP_MAX", db.TEMP_INITIAL < db.TEMP_MAX)
    check("REHEAT_DELTA > 0", db.REHEAT_DELTA > 0)
    check("DISPLACEMENT_COOL_DELTA > 0", db.DISPLACEMENT_COOL_DELTA > 0)
    check("reheat overshoots fresh stores so actively-used memories can "
          "exceed initial", db.TEMP_INITIAL + db.REHEAT_DELTA > db.TEMP_INITIAL)
    check("pressure cooling is additive on top of displacement cooling",
          db.DISPLACEMENT_COOL_DELTA + db.PRESSURE_COOL_DELTA
          > db.DISPLACEMENT_COOL_DELTA)

    # -- Threshold sanity ----------------------------------------------------
    check("0 < DEDUP_SIMILARITY_THRESHOLD < 1",
          0 < db.DEDUP_SIMILARITY_THRESHOLD < 1)
    check("0 < PRESSURE_THRESHOLD < 1",
          0 < db.PRESSURE_THRESHOLD < 1)
    check("PROJECT_MEMORY_CAP > 0", db.PROJECT_MEMORY_CAP > 0)

    # -- _COOL_RATE: critical is sacred --------------------------------------
    check("_COOL_RATE['critical'] == 0.0 (sacred: never cools)",
          db._COOL_RATE["critical"] == 0.0)
    check("_COOL_RATE['high'] == 0.25", db._COOL_RATE["high"] == 0.25)
    check("_COOL_RATE['normal'] == 1.0", db._COOL_RATE["normal"] == 1.0)
    check("_COOL_RATE['low'] == 2.0", db._COOL_RATE["low"] == 2.0)

    # Ordering: lower importance cools faster than higher importance
    check("_COOL_RATE monotonic: low > normal > high > critical",
          db._COOL_RATE["low"] > db._COOL_RATE["normal"]
          > db._COOL_RATE["high"] > db._COOL_RATE["critical"])

    # Four tiers exactly
    check("_COOL_RATE has exactly four importance tiers",
          set(db._COOL_RATE.keys()) == {"critical", "high", "normal", "low"})

    # -- _IMPORTANCE_WEIGHT: search ranking bonuses --------------------------
    check("_IMPORTANCE_WEIGHT['critical'] == 0.15",
          db._IMPORTANCE_WEIGHT["critical"] == 0.15)
    check("_IMPORTANCE_WEIGHT['high'] == 0.10",
          db._IMPORTANCE_WEIGHT["high"] == 0.10)
    check("_IMPORTANCE_WEIGHT['normal'] == 0.0",
          db._IMPORTANCE_WEIGHT["normal"] == 0.0)
    check("_IMPORTANCE_WEIGHT['low'] == -0.05",
          db._IMPORTANCE_WEIGHT["low"] == -0.05)

    # Ordering: higher importance ranks higher in search
    check("_IMPORTANCE_WEIGHT monotonic: critical > high > normal > low",
          db._IMPORTANCE_WEIGHT["critical"] > db._IMPORTANCE_WEIGHT["high"]
          > db._IMPORTANCE_WEIGHT["normal"] > db._IMPORTANCE_WEIGHT["low"])

    check("_IMPORTANCE_WEIGHT has exactly four importance tiers",
          set(db._IMPORTANCE_WEIGHT.keys())
          == {"critical", "high", "normal", "low"})

    # Cross-dict consistency: same four tiers in both
    check("_COOL_RATE and _IMPORTANCE_WEIGHT share the same tier keys",
          set(db._COOL_RATE.keys()) == set(db._IMPORTANCE_WEIGHT.keys()))

    # -- Ranking spread is large enough to matter ----------------------------
    # Critical bonus of 0.15 outweighs a 0.15 similarity gap, so a highly
    # critical memory can beat a slightly more similar non-critical one.
    # Low penalty of -0.05 plus critical bonus of 0.15 makes a 0.20 spread
    # between the two extremes, which should feel meaningful vs. cosine
    # values in the 0.5 to 1.0 range.
    spread = db._IMPORTANCE_WEIGHT["critical"] - db._IMPORTANCE_WEIGHT["low"]
    check("importance ranking spread (critical - low) >= 0.15",
          spread >= 0.15)

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
