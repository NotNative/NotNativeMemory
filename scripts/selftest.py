"""Self-test for NotNativeMemory installation.

Creates a disposable user, exercises the embed + project + store +
search + forget + ingest + rag_search pipeline end-to-end, and cleans
up. A successful run means the model is loadable, pgvector is wired,
RLS-aware paths resolve, and both the memory and RAG tables accept
writes + reads.

Intentionally uses a throwaway user rather than a shared "__self_test__"
account so a selftest run on a populated database does not collide
with any existing user's data. The user (and everything they wrote)
is removed at the end via ON DELETE CASCADE on the users FK.
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()


async def test():
    """Run embed + DB round-trip + RAG round-trip under a scratch user."""
    from lib import auth_db, rls
    from lib.auth_context import set_current_user_id
    from lib.db import (
        get_pool, store_memory, search_memories,
        forget_memory, get_or_create_project, close_pool,
    )
    from lib.embeddings import embed, EMBEDDING_DIM
    from lib.rag.ingest import ingest_text
    from lib.rag.search import search_docs

    # 1. Embedding model loads and produces the declared dim.
    vec = embed("This is a test memory for NotNativeMemory setup.")
    if len(vec) != EMBEDDING_DIM:
        print(f"  Embedding: FAIL (expected {EMBEDDING_DIM} dims, got {len(vec)})")
        sys.exit(1)
    print(f"  Embedding: OK ({EMBEDDING_DIM} dimensions)")

    # 2. Disposable user so DB ops have a real owner without touching
    #    anyone else's data. Random suffix is idempotency insurance:
    #    a prior crashed run does not block a fresh attempt.
    username = f"selftest-{secrets.token_hex(4)}"
    user = await auth_db.create_user(username, "selftest-password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)
    print(f"  User: OK ({username})")

    # 3. Memory stack round trip.
    project_dir = f"/tmp/nnm-selftest-{secrets.token_hex(4)}"
    project_id = await get_or_create_project(
        project_dir, owner_user_id=uid, name="Self Test",
    )
    mem_id = await store_memory(
        content="Self-test memory: the installer is working correctly.",
        embedding=vec,
        project_id=project_id,
        owner_user_id=uid,
        tags=["test"],
        importance="low",
    )
    print(f"  Memory store: OK (id={mem_id})")

    results = await search_memories(
        query_embedding=vec,
        project_id=project_id,
        owner_user_id=uid,
        limit=1,
    )
    if len(results) != 1:
        print(f"  Memory search: FAIL (expected 1 result, got {len(results)})")
        sys.exit(1)
    sim = results[0]["similarity"]
    print(f"  Memory search: OK (similarity={sim})")

    deleted = await forget_memory(mem_id, owner_user_id=uid)
    if not deleted:
        print("  Memory forget: FAIL (memory not deleted)")
        sys.exit(1)
    print("  Memory forget: OK")

    # 4. RAG stack round trip. Ingest a tiny document, search for the
    #    sentinel phrase, assert the top hit carries it.
    sentinel = "selftest sentinel phrase 5a7c"
    ingest_result = await ingest_text(
        owner_user_id=uid,
        project_id=project_id,
        title="Selftest doc",
        content=f"This is a NotNativeMemory install selftest document.\n\n{sentinel}\n",
    )
    if ingest_result.get("status") != "complete":
        print(f"  RAG ingest: FAIL (status={ingest_result.get('status')})")
        sys.exit(1)
    print(f"  RAG ingest: OK ({ingest_result['chunk_count']} chunk(s))")

    rag_hits = await search_docs(
        owner_user_id=uid,
        project_id=project_id,
        query=sentinel,
        limit=1,
    )
    if not rag_hits or sentinel not in (rag_hits[0].get("content") or ""):
        print("  RAG search: FAIL (sentinel phrase not surfaced)")
        sys.exit(1)
    print(f"  RAG search: OK (similarity={rag_hits[0]['similarity']})")

    # 5. Cleanup. DELETE on users cascades through projects,
    #    memories, facts, documents, doc_chunks, ingestion_jobs.
    #    admin_conn bypasses RLS so the cleanup sees all of the
    #    scratch user's rows regardless of the current session GUC.
    pool = await get_pool()
    async with rls.admin_conn(pool) as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
    await close_pool()
    print("  Cleanup: OK")


asyncio.run(test())
print()
print("All self-tests passed!")
