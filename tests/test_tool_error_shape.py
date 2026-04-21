"""
Integration tests for the Phase 3 tool-boundary error wrapping.

Before this change, unexpected exceptions inside an @mcp.tool()
handler propagated raw to the MCP framework, producing unpredictable
client-facing output. The fix catches at the boundary and converts to
a structured {"error": ...} response whose happy-path keys match what
the tool returns on success.

These tests drive two failure paths end-to-end through the real tool
functions:

1. Missing embedding model: call memory_search with _model and
   _model_path pointing at a path that does not exist. The tool
   should return {"error": "FileNotFoundError: ...", "results": [],
   "count": 0}, NOT raise.

2. DB helper raises: monkeypatch lib.db.forget_memory to raise an
   asyncpg ConnectionDoesNotExistError. memory_forget should return
   {"error": "ConnectionDoesNotExistError: ...", "forgotten": False},
   NOT raise.

Happy-path sanity: memory_forget on a random UUID returns
{"forgotten": False} with no "error" key. Confirms the wrap does not
accidentally flag a clean "nothing to delete" as an error.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)

Usage:
    python tests/test_tool_error_shape.py
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
    import asyncpg
    import server
    from lib import auth_db, db, embeddings, rls
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
    test_username = f"toolerr-{run_id}"

    pool = await db.get_pool()
    user = await auth_db.create_user(test_username, "password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)

    # Save everything we monkeypatch so the finally block restores
    # the process back to the state we found it in.
    saved_model = embeddings._model
    saved_model_path = embeddings._model_path
    saved_forget = db.forget_memory

    try:
        # ================================================================
        # Scenario 1: missing embedding model surfaces as structured
        # error, not raw exception
        # ================================================================
        embeddings._model = None
        embeddings._model_path = "/nonexistent-nnm-test-path"

        result = await server.memory_search(
            query="anything",
            limit=5,
        )
        check("memory_search with bad model path: returns a dict "
              "(did not raise)", isinstance(result, dict))
        check("memory_search with bad model path: 'error' key present",
              "error" in result)
        check("memory_search with bad model path: happy-path keys "
              "still present",
              "results" in result and "count" in result)
        check("memory_search with bad model path: empty results",
              result.get("results") == [] and result.get("count") == 0)
        check("memory_search with bad model path: error names the "
              "exception type",
              "FileNotFoundError" in result.get("error", ""))

        # Restore so later scenarios can use embeddings (not needed
        # here but keeps the invariant tidy).
        embeddings._model = saved_model
        embeddings._model_path = saved_model_path

        # ================================================================
        # Scenario 2: DB helper raises -> memory_forget returns
        # structured error
        # ================================================================
        async def boom(*args, **kwargs):
            raise asyncpg.exceptions.ConnectionDoesNotExistError(
                "simulated DB failure"
            )
        db.forget_memory = boom

        result2 = await server.memory_forget(str(uuid4()))
        check("memory_forget with broken DB: returns a dict "
              "(did not raise)", isinstance(result2, dict))
        check("memory_forget with broken DB: 'error' key present",
              "error" in result2)
        check("memory_forget with broken DB: happy-path key present",
              "forgotten" in result2)
        check("memory_forget with broken DB: forgotten=False",
              result2.get("forgotten") is False)
        check("memory_forget with broken DB: error names the "
              "exception type",
              "ConnectionDoesNotExistError" in result2.get("error", ""))

        # Restore the real forget_memory before the happy-path test.
        db.forget_memory = saved_forget

        # ================================================================
        # Scenario 3: happy-path sanity. memory_forget on an unknown
        # UUID returns {"forgotten": False} with NO "error" key. The
        # wrapper must not mistake a clean "nothing to delete" for an
        # exception.
        # ================================================================
        result3 = await server.memory_forget(str(uuid4()))
        check("memory_forget on unknown UUID: no error key",
              "error" not in result3)
        check("memory_forget on unknown UUID: forgotten=False",
              result3.get("forgotten") is False)

        # ================================================================
        # Scenario 4: bad UUID format still returns the original
        # pre-existing structured error (not the new generic wrap).
        # Belt-and-suspenders: confirms the wrap did not cannibalize
        # the existing ValueError handling.
        # ================================================================
        result4 = await server.memory_forget("not-a-uuid")
        check("memory_forget on bad UUID: forgotten=False",
              result4.get("forgotten") is False)
        check("memory_forget on bad UUID: pre-existing format error "
              "preserved",
              "Invalid memory ID format" in result4.get("error", ""))

    finally:
        embeddings._model = saved_model
        embeddings._model_path = saved_model_path
        db.forget_memory = saved_forget

        async with rls.admin_conn(pool) as conn:
            await conn.execute(
                "DELETE FROM memories WHERE owner_user_id = $1", uid,
            )
            await conn.execute(
                "DELETE FROM projects WHERE owner_user_id = $1", uid,
            )
        await pool.execute("DELETE FROM users WHERE id = $1", uid)
        await db.close_pool()

    print("---")
    print("all passed" if failed == 0 else f"{failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
