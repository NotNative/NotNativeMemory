"""
End-to-end round-trip test for the RAG ingest + search pipeline.

Creates an isolated test user, ingests a short text blob with a
deliberately unusual phrase, runs rag_search, and asserts the phrase
surfaces as the top hit. Also exercises the dedup path by re-ingesting
the same content and confirming no new chunks are written.

Does not exercise the MCP transport; it calls lib.rag directly, which
is the same codepath the tool handlers use. Tool-handler-specific
concerns (auth_context, _tool_error wrapping) are covered by
test_tool_error_shape.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)
    MEMORY_MODEL_PATH                            (loaded; embeds real text)

Usage:
    python tests/test_rag_roundtrip.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


# Intentionally weird phrase so the cosine similarity signal is
# unambiguous even against whatever other docs live in the test DB.
# If this phrase appears somewhere in the corpus unrelated to this
# test, rename it, don't loosen the threshold.
UNIQUE_PHRASE = "quantum teapot recalibration 9f3a"

DOC_CONTENT = f"""\
# Roundtrip Test Document

This document exists only to verify that the RAG ingestion pipeline
round-trips correctly. The unique sentinel phrase below should come
back as the top chunk when the corresponding query is issued.

{UNIQUE_PHRASE}

The remaining paragraphs are filler so the chunker produces more than
one chunk. Fixed-window chunking at 2000 characters with 250 overlap
means a document shorter than ~1750 characters is one chunk, so this
filler pushes us toward the multi-chunk case when the default size
applies. The exact layout is not load-bearing; only the presence of
the sentinel phrase matters for this round-trip assertion.
"""


async def run() -> int:
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id
    from lib.rag.ingest import ingest_text
    from lib.rag.search import search_docs

    failed = 0
    total = 0

    def check(label, condition):
        nonlocal failed, total
        total += 1
        if condition:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}")
            failed += 1

    test_username = f"rag-test-{secrets.token_hex(4)}"
    test_project_dir = f"/tmp/rag-test-{secrets.token_hex(4)}"

    pool = await db.get_pool()

    user = await auth_db.create_user(test_username, "password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)

    project_id = await db.get_or_create_project(test_project_dir, uid)

    # 1. First ingestion: happy path.
    result = await ingest_text(
        owner_user_id=uid,
        project_id=project_id,
        title="Roundtrip sentinel doc",
        content=DOC_CONTENT,
        source_uri=None,
    )
    check("first ingestion status is 'complete'",
          result.get("status") == "complete")
    check("first ingestion returned a document_id",
          bool(result.get("document_id")))
    check("first ingestion wrote at least one chunk",
          (result.get("chunk_count") or 0) >= 1)

    document_id = UUID(result["document_id"])

    # Verify doc_chunks rows exist for this document under admin_conn
    # (bypasses RLS so we're testing what's actually stored, not just
    # what's visible).
    async with rls.admin_conn(pool) as conn:
        row_count = await conn.fetchval(
            "SELECT COUNT(*) FROM doc_chunks WHERE document_id = $1",
            document_id,
        )
    check("doc_chunks row count matches ingestion result",
          row_count == result["chunk_count"])

    # 2. Round-trip search: query should find the unique phrase chunk.
    hits = await search_docs(
        owner_user_id=uid,
        project_id=project_id,
        query=UNIQUE_PHRASE,
        limit=3,
    )
    check("search returned at least one hit", len(hits) >= 1)

    if hits:
        top = hits[0]
        check("top hit carries the unique phrase",
              UNIQUE_PHRASE in (top.get("content") or ""))
        check("top hit document_id matches what was just ingested",
              top.get("document_id") == str(document_id))
        check("top hit has a similarity score above 0.5",
              (top.get("similarity") or 0.0) > 0.5)
        check("top hit carries document title",
              top.get("document_title") == "Roundtrip sentinel doc")

    # 3. Dedup: re-ingesting the same content should be a no-op.
    second = await ingest_text(
        owner_user_id=uid,
        project_id=project_id,
        title="Roundtrip sentinel doc (retry)",
        content=DOC_CONTENT,
        source_uri=None,
    )
    check("second ingestion status is 'deduplicated'",
          second.get("status") == "deduplicated")
    check("second ingestion returns the same document_id",
          second.get("document_id") == str(document_id))
    check("second ingestion did no chunk work",
          second.get("chunk_count") == 0)

    # doc_chunks count should be unchanged after dedup.
    async with rls.admin_conn(pool) as conn:
        row_count_after = await conn.fetchval(
            "SELECT COUNT(*) FROM doc_chunks WHERE document_id = $1",
            document_id,
        )
    check("dedup did not add chunks",
          row_count_after == row_count)

    # 4. RLS isolation: a different user's search must not see this doc.
    other_username = f"rag-other-{secrets.token_hex(4)}"
    other = await auth_db.create_user(other_username, "password-1234")
    other_uid = UUID(other["id"])
    other_project_id = await db.get_or_create_project(
        f"/tmp/rag-other-{secrets.token_hex(4)}", other_uid,
    )
    other_hits = await search_docs(
        owner_user_id=other_uid,
        project_id=other_project_id,
        query=UNIQUE_PHRASE,
        limit=5,
    )
    check("cross-user search does not see the other user's chunks",
          all(h.get("document_id") != str(document_id) for h in other_hits))

    # 5. Empty query returns empty results rather than raising.
    empty_hits = await search_docs(
        owner_user_id=uid,
        project_id=project_id,
        query="   ",
        limit=5,
    )
    check("empty query returns empty list", empty_hits == [])

    # Cleanup. CASCADE on users -> projects -> documents -> doc_chunks
    # handles the graph, but we explicitly wipe so the test is
    # self-contained even if a future cascade weakens.
    async with rls.admin_conn(pool) as conn:
        await conn.execute("DELETE FROM ingestion_jobs WHERE document_id = $1",
                           document_id)
        await conn.execute("DELETE FROM doc_chunks WHERE document_id = $1",
                           document_id)
        await conn.execute("DELETE FROM documents WHERE id = $1", document_id)
        await conn.execute("DELETE FROM projects WHERE id = $1", project_id)
        await conn.execute("DELETE FROM projects WHERE id = $1",
                           other_project_id)
    await pool.execute("DELETE FROM users WHERE id = ANY($1)",
                       [uid, other_uid])
    await db.close_pool()

    print("---")
    print(f"{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
