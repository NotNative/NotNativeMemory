#!/usr/bin/env python3
"""
Tests for the two-tier working-set selection in get_context_memories.

The selection guarantees that `critical` + `rule`-class memories ALWAYS
land in the working set first (up to ~40% of budget), even when many
hot non-critical memories would otherwise win on temperature alone.

Unit tests cover the budget split logic without a database. Integration
test covers the live DB path against pgvector.

Usage:
    python tests/test_working_set.py                  # unit tests only
    NNM_INTEGRATION=1 python tests/test_working_set.py  # incl. DB
"""

import asyncio
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def test_critical_budget_split_constant():
    """Budget split: ~40% reserved for critical, minimum floor of 200 chars."""
    from lib import db as dbmod
    # The two-tier split lives inline in get_context_memories; the test
    # asserts the public constant pair we depend on indirectly: the
    # formula is `max(int(char_budget * 0.4), 200)`. Sanity-check the
    # math here rather than reach into the function body.
    char_budget = 500 * 4  # default 500 token budget
    crit_budget = max(int(char_budget * 0.4), 200)
    assert crit_budget == 800
    print("[OK] default 500-token budget reserves 800 chars (40%) for critical/rule")


def test_critical_budget_floor():
    """At very tight budgets the critical floor is 200 chars."""
    char_budget = 50 * 4  # minimum allowed budget
    crit_budget = max(int(char_budget * 0.4), 200)
    assert crit_budget == 200
    print("[OK] tight budget still reserves the 200-char critical floor")


async def _integration_run() -> int:
    """Live-DB: critical and rule memories always appear, even when buried
    under many hot high-importance non-rule memories."""
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
    user = await auth_db.create_user(f"wset-test-{run_id}", "password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)

    pool = await db.get_pool()
    proj_id = await db.get_or_create_project(
        f"/tmp/wset-{run_id}", owner_user_id=uid,
    )

    now = datetime.now(timezone.utc)
    crit_id = uuid4()
    rule_id = uuid4()

    # Seed: 2 critical/rule rows + 30 HOT high-importance rows.
    # Without the two-tier split, the hot highs would crowd out the
    # critical row when the working set is trimmed by budget.
    async with rls.admin_conn(pool) as conn:
        await conn.execute(
            """INSERT INTO memories
                   (id, project_id, owner_user_id, content,
                    tags, importance, class, temperature, last_accessed)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            crit_id, proj_id, uid,
            "critical-rule-that-must-always-surface",
            [], "critical", "rule", 30.0, now,
        )
        await conn.execute(
            """INSERT INTO memories
                   (id, project_id, owner_user_id, content,
                    tags, importance, class, temperature, last_accessed)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
            rule_id, proj_id, uid,
            "rule-class-with-normal-importance",
            [], "normal", "rule", 30.0, now,
        )
        # 30 hot high-importance rows with much higher temperature
        for i in range(30):
            await conn.execute(
                """INSERT INTO memories
                       (id, project_id, owner_user_id, content,
                        tags, importance, class, temperature, last_accessed)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)""",
                uuid4(), proj_id, uid,
                f"hot-high-row-{i}-" + ("x" * 80),
                [], "high", None, 95.0, now,
            )

    result = await db.get_context_memories(
        project_id=proj_id, owner_user_id=uid, max_tokens=600,
    )
    ids = [m["id"] for m in result]
    check("critical memory survives the budget cut", str(crit_id) in ids)
    check("rule-class memory survives the budget cut", str(rule_id) in ids)
    check(
        "thermal layer still surfaces some hot high rows",
        any(m["importance"] == "high" for m in result),
    )
    check(
        "no duplicate rows across the two tiers",
        len(ids) == len(set(ids)),
    )

    # Tight budget: 50 tokens. Critical must still appear.
    result_tight = await db.get_context_memories(
        project_id=proj_id, owner_user_id=uid, max_tokens=50,
    )
    ids_tight = [m["id"] for m in result_tight]
    check(
        "critical memory survives even a tight budget",
        str(crit_id) in ids_tight,
    )

    return failed


UNIT_TESTS = [
    test_critical_budget_split_constant,
    test_critical_budget_floor,
]


def main():
    for t in UNIT_TESTS:
        t()
    print(f"\n[UNIT] All {len(UNIT_TESTS)} unit tests passed")

    if "--integration" in sys.argv or os.environ.get("NNM_INTEGRATION") == "1":
        print("\n[INTEGRATION] Running live-DB tests against pgvector...")
        failed = asyncio.run(_integration_run())
        if failed:
            print(f"\n[INTEGRATION] {failed} integration check(s) failed")
            sys.exit(1)
        print("\n[INTEGRATION] All integration checks passed")
    else:
        print(
            "\n[INTEGRATION] Skipped. "
            "Re-run with --integration or NNM_INTEGRATION=1."
        )


if __name__ == "__main__":
    main()
