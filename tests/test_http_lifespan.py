"""
Regression test for the HTTP-mode lifespan that starts the async
ingestion worker.

The original bug: install_rag_worker_lifespan used the
add_event_handler("startup", ...) API that Starlette 1.0 removed,
so `python server.py` crashed on first request with AttributeError
the moment uvicorn tried to dispatch lifespan events.

This test drives install_rag_worker_lifespan against the real
FastMCP app and exercises the lifespan_context to verify:

- The helper installs without raising (no removed-API surprises).
- During lifespan startup, the worker task is created and named.
- The worker keeps running while the lifespan is held.
- During lifespan shutdown, the worker stops cleanly.
- The wrap composes with FastMCP's own lifespan so we don't drop
  whatever startup/shutdown the framework needed.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)
    MEMORY_MODEL_PATH                            (loaded at worker startup)

Usage:
    python tests/test_http_lifespan.py
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


async def run() -> int:
    import server
    from lib import db

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

    # Fresh app per test invocation. Calling streamable_http_app() twice
    # on the same FastMCP instance returns separate Starlette apps; we
    # do not share with any other test.
    app = server.mcp.streamable_http_app()

    # Pre-condition: Starlette must still expose the modern lifespan
    # contract this helper relies on. If a future Starlette version
    # changes this, we want to learn here, not in a stack trace from
    # uvicorn at server start.
    check("app.router exposes lifespan_context",
          hasattr(app.router, "lifespan_context"))

    # The helper itself must not raise at install time. The legacy
    # add_event_handler call would have failed here.
    server.install_rag_worker_lifespan(app)
    check("install_rag_worker_lifespan returned without raising", True)

    check("rag_worker state attached to app.state",
          hasattr(app.state, "rag_worker"))

    state = app.state.rag_worker
    check("worker not yet running before lifespan startup",
          state.get("task") is None)

    # Drive the lifespan exactly the way uvicorn does on startup.
    async with app.router.lifespan_context(app):
        check("worker task exists after lifespan startup",
              state.get("task") is not None)
        if state.get("task") is not None:
            check("worker task is named 'rag-worker'",
                  state["task"].get_name() == "rag-worker")
            check("worker task is running (not done) inside lifespan",
                  not state["task"].done())

    # After lifespan exits, the worker must have stopped.
    check("worker task is done after lifespan shutdown",
          state.get("task") is not None and state["task"].done())

    # Pool was opened by the worker startup; close it so we leave the
    # process state clean for any test runner that re-imports server.
    await db.close_pool()

    print("---")
    print(f"{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
