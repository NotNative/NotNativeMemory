"""
Integration tests for the async RAG ingestion path.

Covers:
- async_mode=True returns immediately with status='queued' and chunks
  inserted with embedding=NULL.
- process_queued_jobs claims and backfills embeddings.
- search_docs surfaces nothing until embeddings are filled in, then
  returns the expected hit.
- recover_stale_jobs re-queues jobs stuck in 'running'.
- _embed_chunks_for_job is idempotent against an already-complete job.
- Sync path (async_mode=False) still works end to end.

All assertions are deterministic. The worker loop itself is not
exercised here — tests call process_queued_jobs / recover_stale_jobs
directly so no polling timing enters the picture.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)
    MEMORY_MODEL_PATH                            (loaded; embeds real text)

Usage:
    python tests/test_rag_async_ingest.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


SENTINEL_PHRASE = "async ingestion sentinel 6b2f"


async def run() -> int:
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id
    from lib.rag.ingest import (
        ingest_text,
        _embed_chunks_for_job,
    )
    from lib.rag.search import search_docs
    from lib.rag.worker import (
        process_queued_jobs,
        recover_stale_jobs,
    )

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

    run_id = secrets.token_hex(4)
    test_username = f"async-ing-{run_id}"
    project_dir = f"/tmp/async-ing-{run_id}"

    pool = await db.get_pool()
    user = await auth_db.create_user(test_username, "password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)

    project_id = await db.get_or_create_project(project_dir, owner_user_id=uid)

    content = (
        "This document tests the async ingestion path in NotNativeMemory.\n\n"
        f"{SENTINEL_PHRASE}\n\n"
        "Filler so the chunker has material to work with, though a single "
        "chunk is also an acceptable outcome for this test."
    )

    # 1. Enqueue with async_mode=True. Caller gets back status='queued'
    # immediately, chunks exist with NULL embeddings, no worker has
    # run yet.
    result = await ingest_text(
        owner_user_id=uid,
        project_id=project_id,
        title="Async roundtrip doc",
        content=content,
        async_mode=True,
    )
    check("async ingest returns status='queued'",
          result.get("status") == "queued")
    check("async ingest reports chunk_count >= 1",
          (result.get("chunk_count") or 0) >= 1)
    document_id = UUID(result["document_id"])
    job_id = UUID(result["ingestion_job_id"])

    async with rls.admin_conn(pool) as conn:
        null_count = await conn.fetchval(
            "SELECT COUNT(*) FROM doc_chunks WHERE document_id = $1 AND embedding IS NULL",
            document_id,
        )
        non_null_count = await conn.fetchval(
            "SELECT COUNT(*) FROM doc_chunks WHERE document_id = $1 AND embedding IS NOT NULL",
            document_id,
        )
        job_status = await conn.fetchval(
            "SELECT status FROM ingestion_jobs WHERE id = $1", job_id,
        )
    check("all chunks have NULL embedding before worker runs",
          null_count >= 1 and non_null_count == 0)
    check("job is in 'queued' state before worker runs",
          job_status == "queued")

    # 2. Before the worker fills in embeddings, search must not return
    # the sentinel: search_docs filters embedding IS NOT NULL.
    pre_hits = await search_docs(
        owner_user_id=uid, project_id=project_id,
        query=SENTINEL_PHRASE, limit=3,
    )
    check("search returns no hit while embeddings are NULL",
          all(h.get("document_id") != str(document_id) for h in pre_hits))

    # 3. Drive the worker one pass. process_queued_jobs returns how
    # many jobs it handled; we should see exactly 1.
    processed = await process_queued_jobs(pool, limit=5)
    check("process_queued_jobs handled the queued job",
          processed == 1)

    async with rls.admin_conn(pool) as conn:
        null_after = await conn.fetchval(
            "SELECT COUNT(*) FROM doc_chunks WHERE document_id = $1 AND embedding IS NULL",
            document_id,
        )
        job_row = await conn.fetchrow(
            "SELECT status, chunk_count, finished_at FROM ingestion_jobs WHERE id = $1",
            job_id,
        )
    check("no chunks remain with NULL embedding after worker",
          null_after == 0)
    check("job transitioned to 'complete'",
          job_row["status"] == "complete")
    check("job finished_at is set after completion",
          job_row["finished_at"] is not None)

    # 4. Search now surfaces the sentinel.
    post_hits = await search_docs(
        owner_user_id=uid, project_id=project_id,
        query=SENTINEL_PHRASE, limit=3,
    )
    check("search surfaces the sentinel after worker run",
          post_hits and str(post_hits[0]["document_id"]) == str(document_id))
    check("top hit content carries the sentinel phrase",
          post_hits and SENTINEL_PHRASE in (post_hits[0].get("content") or ""))

    # 5. Idempotency: running the worker again finds nothing to do.
    processed_again = await process_queued_jobs(pool, limit=5)
    check("second worker pass finds zero queued jobs",
          processed_again == 0)

    # 6. _embed_chunks_for_job on an already-complete job is a no-op
    # that does not crash. Returns 0 pending chunks handled.
    handled = await _embed_chunks_for_job(pool, uid, job_id)
    check("_embed_chunks_for_job on complete job returns 0",
          handled == 0)

    # 7. recover_stale_jobs re-queues jobs stuck in 'running'. Simulate
    # a crashed-mid-embed scenario: manually set the job status back
    # to 'running' and NULL out one chunk's embedding.
    async with rls.admin_conn(pool) as conn:
        one_chunk = await conn.fetchval(
            "SELECT id FROM doc_chunks WHERE document_id = $1 ORDER BY chunk_index LIMIT 1",
            document_id,
        )
        await conn.execute(
            "UPDATE doc_chunks SET embedding = NULL WHERE id = $1", one_chunk,
        )
        await conn.execute(
            "UPDATE ingestion_jobs SET status = 'running', finished_at = NULL WHERE id = $1",
            job_id,
        )

    recovered = await recover_stale_jobs(pool)
    check("recover_stale_jobs flipped the stuck job back to queued",
          recovered >= 1)

    async with rls.admin_conn(pool) as conn:
        status_after_recover = await conn.fetchval(
            "SELECT status FROM ingestion_jobs WHERE id = $1", job_id,
        )
    check("job status is 'queued' after recovery",
          status_after_recover == "queued")

    # Now run the worker again — it should pick up the recovered job
    # and backfill the one NULL embedding.
    processed_recovered = await process_queued_jobs(pool, limit=5)
    check("worker handled the recovered job",
          processed_recovered == 1)

    async with rls.admin_conn(pool) as conn:
        final_null_count = await conn.fetchval(
            "SELECT COUNT(*) FROM doc_chunks WHERE document_id = $1 AND embedding IS NULL",
            document_id,
        )
    check("all embeddings filled after recovery + worker run",
          final_null_count == 0)

    # 8. Sync path still works: async_mode=False returns
    # status='complete' immediately with chunks fully embedded.
    sync_result = await ingest_text(
        owner_user_id=uid,
        project_id=project_id,
        title="Sync roundtrip doc",
        content="A smaller document for the sync path sanity check.",
        async_mode=False,
    )
    check("sync ingest returns status='complete'",
          sync_result.get("status") == "complete")

    sync_doc_id = UUID(sync_result["document_id"])
    async with rls.admin_conn(pool) as conn:
        sync_null_count = await conn.fetchval(
            "SELECT COUNT(*) FROM doc_chunks WHERE document_id = $1 AND embedding IS NULL",
            sync_doc_id,
        )
    check("sync ingest left no NULL embeddings",
          sync_null_count == 0)

    # Cleanup. User delete cascades to projects, documents,
    # doc_chunks, ingestion_jobs.
    async with rls.admin_conn(pool) as conn:
        await conn.execute("DELETE FROM users WHERE id = $1", uid)
    await db.close_pool()

    print("---")
    print(f"{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
