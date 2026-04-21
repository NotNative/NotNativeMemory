"""
Regression tests for the migration runner's failure behavior.

Before the Phase 2 follow-up, a syntactically bad migration file was
logged and swallowed, so the server would start with a half-applied
schema and users hit cryptic runtime errors. The fix propagates
migration errors so the server refuses to start.

These tests point _MIGRATIONS_DIR at a temporary directory with
controlled content, so the repo's real config/migrations/ is never
mutated. The connection is built directly via asyncpg.connect
instead of via get_pool() so the test never takes a dependency on
pool state from earlier tests.

Covered:
- A broken migration file causes _run_migrations_on_conn to raise.
- The broken migration is NOT recorded in schema_migrations (the
  per-file transaction rolls back).
- On a subsequent run with a fixed file, the migration applies
  cleanly and the server can proceed.
- A successful migration IS recorded, preventing re-apply.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_migration_failures.py
"""

import asyncio
import os
import secrets
import shutil
import sys
import tempfile

import asyncpg

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


async def _connect():
    """Open a one-shot migration-role connection, same env vars that
    get_pool() reads. Load .env first so this works outside a server
    process that already populated the environment."""
    from dotenv import load_dotenv
    load_dotenv()
    return await asyncpg.connect(
        host=os.environ.get("MEMORY_DB_HOST", "localhost"),
        port=int(os.environ.get("MEMORY_DB_PORT", "5433")),
        database=os.environ.get("MEMORY_DB_NAME", "notnative_memory"),
        user=os.environ.get("MEMORY_DB_USER", "memory"),
        password=os.environ.get("MEMORY_DB_PASSWORD", ""),
    )


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

    # Unique marker for this run's test migration filenames so reruns
    # never collide and the test leaves zero trace even on failure.
    marker = secrets.token_hex(4)
    bad_name = f"zzz_bad_{marker}.sql"
    good_name = f"zzz_good_{marker}.sql"
    tmp_table = f"zzz_migration_test_{marker}"

    tmpdir = tempfile.mkdtemp(prefix="nnm_mig_test_")
    original_mig_dir = db._MIGRATIONS_DIR
    mig_conn = None
    try:
        # Seed tmpdir with a syntactically invalid migration before
        # redirecting the runner.
        with open(os.path.join(tmpdir, bad_name), "w", encoding="utf-8") as f:
            f.write("NOT VALID SQL AT ALL;\n")

        db._MIGRATIONS_DIR = tmpdir

        mig_conn = await _connect()

        # Scenario 1: bad migration raises
        raised = False
        try:
            await db._run_migrations_on_conn(mig_conn)
        except Exception:
            raised = True
        check("broken migration file: _run_migrations_on_conn raises",
              raised)

        # Scenario 2: broken migration is not recorded
        recorded = await mig_conn.fetchval(
            "SELECT COUNT(*) FROM schema_migrations WHERE filename = $1",
            bad_name,
        )
        check("broken migration: not recorded in schema_migrations "
              "(transaction rolled back)", recorded == 0)

        # Scenario 3: retry with a fixed file applies cleanly
        os.remove(os.path.join(tmpdir, bad_name))
        with open(os.path.join(tmpdir, good_name), "w",
                  encoding="utf-8") as f:
            f.write(f"CREATE TABLE {tmp_table} (id INT);\n")

        applied = await db._run_migrations_on_conn(mig_conn)
        check("retry with fixed migration: applies exactly one file",
              applied == 1)

        # Scenario 4: good migration is recorded
        recorded_good = await mig_conn.fetchval(
            "SELECT COUNT(*) FROM schema_migrations WHERE filename = $1",
            good_name,
        )
        check("good migration: recorded in schema_migrations",
              recorded_good == 1)

        # Scenario 5: subsequent run with nothing new returns 0
        applied_again = await db._run_migrations_on_conn(mig_conn)
        check("third run (no pending): applies zero migrations",
              applied_again == 0)

        # Scenario 6: the DDL inside the good migration was committed
        table_rows = await mig_conn.fetchval(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name = $1",
            tmp_table,
        )
        check("good migration: schema change committed "
              "(test table exists)", table_rows == 1)

        # Cleanup: drop the test table and the schema_migrations row we
        # inserted so this test leaves zero trace even on success.
        await mig_conn.execute(f"DROP TABLE IF EXISTS {tmp_table}")
        await mig_conn.execute(
            "DELETE FROM schema_migrations WHERE filename = $1",
            good_name,
        )
    finally:
        if mig_conn is not None:
            await mig_conn.close()
        db._MIGRATIONS_DIR = original_mig_dir
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
