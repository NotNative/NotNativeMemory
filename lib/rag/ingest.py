"""
Ingestion pipeline for RAG documents.

Two modes:

- **Sync** (default): the caller blocks until the document is fully
  chunked, embedded, and stored. Used when the content is small
  enough that blocking is fine.
- **Async** (``async_mode=True``): the caller returns immediately
  after the chunks are inserted with NULL embeddings and the
  ingestion_job is left in ``status='queued'``. A background worker
  (lib/rag/worker.py) backfills the embeddings. Used for large
  documents or bulk ingestion.

Both modes share ``_embed_chunks_for_job``. The sync path calls it
inline; the worker calls it after claiming a queued job.

Dedup is per owner via sha256 over the raw content. Re-ingesting the
same bytes is a no-op that returns the existing document_id; callers
who want to force a re-embed delete the document first.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from lib.rag.chunking import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    chunk_text,
)


# Extension -> MIME heuristic for ingest_file. The table is intentionally
# short: Phase A is plain-text only. Unknown extensions fall through to
# text/plain and the file is still read as UTF-8. Binary formats (PDF,
# docx, etc.) are out of scope until a dedicated extractor lands.
_EXTENSION_CONTENT_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".rst": "text/plain",
    ".log": "text/plain",
}


def _sha256_of(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def _insert_document_and_chunks(
    conn,
    owner_user_id: UUID,
    project_id: UUID,
    title: str,
    content: str,
    source_uri: Optional[str],
    content_type: str,
    sha: str,
    size_bytes: int,
    metadata: Dict[str, Any],
    chunks: List[Tuple[int, str, int, int]],
    job_status: str,
) -> Tuple[UUID, UUID]:
    """
    Insert a new document row, its chunks (with embedding=NULL), and a
    fresh ingestion_job in ``job_status``. Returns (document_id, job_id).

    Caller holds the rls.app_conn context. Runs every INSERT against
    the same connection so they are implicitly grouped; pgvector's
    NULL-embedding chunks are valid because the column is nullable.
    """
    doc_row = await conn.fetchrow(
        """
        INSERT INTO documents
            (project_id, owner_user_id, title, source_uri,
             content_type, sha256, size_bytes, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
        RETURNING id
        """,
        project_id, owner_user_id, title, source_uri,
        content_type, sha, size_bytes, json.dumps(metadata),
    )
    document_id = doc_row["id"]

    if chunks:
        rows = [
            (document_id, project_id, owner_user_id,
             idx, text, start, end)
            for (idx, text, start, end) in chunks
        ]
        await conn.executemany(
            """
            INSERT INTO doc_chunks
                (document_id, project_id, owner_user_id,
                 chunk_index, content, char_start, char_end)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            rows,
        )

    # started_at only gets populated when we transition to running, so
    # queued jobs have it NULL. chunk_count is the *target* count; for
    # queued jobs it reports what we intend to embed, not what's done.
    started_at_clause = "now()" if job_status == "running" else "NULL"
    job_row = await conn.fetchrow(
        f"""
        INSERT INTO ingestion_jobs
            (document_id, owner_user_id, status, started_at, chunk_count)
        VALUES ($1, $2, $3, {started_at_clause}, $4)
        RETURNING id
        """,
        document_id, owner_user_id, job_status, len(chunks),
    )
    return document_id, job_row["id"]


async def _embed_chunks_for_job(
    pool, owner_user_id: UUID, job_id: UUID,
) -> int:
    """
    Backfill embeddings for a single ingestion_job.

    Idempotent: reads only doc_chunks with embedding IS NULL so a
    partial prior run is fine. Transitions the job to 'running' on
    entry and 'complete' on success. On any embedding or DB failure,
    marks the job 'failed' with the exception text and re-raises.

    Returns the number of chunks embedded by this call (0 if none
    pending). Callers can use that to detect "already done."

    This is the shared path between the sync ingest_text path and the
    background worker in lib/rag/worker.py. Both must leave identical
    side effects; keep the logic here rather than duplicating.
    """
    from lib import rls
    from lib.embeddings import embed_batch

    # Grab the document_id + pending chunk list in one short trip so
    # we release the connection before the slow embed pass.
    async with rls.app_conn(pool, owner_user_id) as conn:
        await conn.execute(
            """
            UPDATE ingestion_jobs
               SET status = 'running',
                   started_at = COALESCE(started_at, now())
             WHERE id = $1
            """,
            job_id,
        )
        job_row = await conn.fetchrow(
            "SELECT document_id FROM ingestion_jobs WHERE id = $1",
            job_id,
        )
        if job_row is None:
            # Job was deleted out from under us. Nothing to do.
            return 0
        document_id = job_row["document_id"]

        pending = await conn.fetch(
            """
            SELECT id, content
              FROM doc_chunks
             WHERE document_id = $1 AND embedding IS NULL
             ORDER BY chunk_index
            """,
            document_id,
        )

    # Embed outside the connection so the pool slot is available for
    # other work while we burn CPU.
    if pending:
        try:
            vectors = embed_batch([r["content"] for r in pending])
        except Exception as exc:
            async with rls.app_conn(pool, owner_user_id) as conn:
                await conn.execute(
                    """
                    UPDATE ingestion_jobs
                       SET status = 'failed',
                           error = $2,
                           finished_at = now()
                     WHERE id = $1
                    """,
                    job_id, f"{type(exc).__name__}: {exc}",
                )
            raise
    else:
        vectors = []

    # Write the embeddings + mark complete under one connection. Keep
    # the UPDATE executemany separate from the finishing UPDATE so a
    # partial write still leaves the chunks filled in and only the
    # job status reflects the inconsistency on retry.
    async with rls.app_conn(pool, owner_user_id) as conn:
        if pending:
            update_rows = [
                (row["id"], str(vec))
                for row, vec in zip(pending, vectors)
            ]
            await conn.executemany(
                "UPDATE doc_chunks SET embedding = $2::vector WHERE id = $1",
                update_rows,
            )
        await conn.execute(
            """
            UPDATE ingestion_jobs
               SET status = 'complete',
                   finished_at = now()
             WHERE id = $1
            """,
            job_id,
        )

    return len(pending)


async def ingest_text(
    owner_user_id: UUID,
    project_id: UUID,
    title: str,
    content: str,
    source_uri: Optional[str] = None,
    content_type: str = "text/plain",
    metadata: Optional[Dict[str, Any]] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    async_mode: bool = False,
) -> Dict[str, Any]:
    """
    Ingest a text blob into documents + doc_chunks.

    Always inserts chunks with NULL embeddings up front. In sync mode
    (default) the embeddings are filled in inline before returning; in
    async mode the job is left queued for the worker and the caller
    gets back immediately.

    Dedup: if (owner_user_id, sha256) already exists, returns the
    existing document_id with status='deduplicated' and no chunk work
    done.

    Args:
        owner_user_id: Caller identity. Required for RLS + dedup scope.
        project_id: Target project. Resolved by the caller via
            get_or_create_project on the scope directory.
        title: Human-readable display name for the document.
        content: The full document text (UTF-8 string).
        source_uri: Where the content came from (file path, URL, or
            None for pasted text). Stored verbatim; not interpreted.
        content_type: MIME hint for the payload. Default text/plain.
        metadata: Arbitrary JSON-serializable dict saved on the
            document row.
        chunk_size, overlap: Passed through to chunk_text.
        async_mode: If True, return after queueing; worker fills in
            embeddings. If False (default), embed inline.

    Returns:
        Dict with document_id, sha256, chunk_count, status, and the
        ingestion_job_id. status is one of "complete" (sync, success),
        "queued" (async, pending worker), or "deduplicated".

    Raises:
        ValueError: If content or title is empty.
        Any exception from the embedder or DB (sync mode only): the
        ingestion_job is marked 'failed' before re-raise.
    """
    if not content or not content.strip():
        raise ValueError("content cannot be empty")
    if not title or not title.strip():
        raise ValueError("title cannot be empty")

    from lib import rls
    from lib.db import get_pool

    sha = _sha256_of(content)
    size_bytes = len(content.encode("utf-8"))
    meta_json = metadata or {}

    pool = await get_pool()

    # Dedup check. If an existing document shares sha256, return early
    # so re-ingestion is a cheap no-op. Running the full chunk+embed
    # pass on a duplicate would just waste CPU and churn pgvector.
    async with rls.app_conn(pool, owner_user_id) as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM documents WHERE owner_user_id = $1 AND sha256 = $2",
            owner_user_id, sha,
        )
        if existing:
            return {
                "document_id": str(existing["id"]),
                "sha256": sha,
                "chunk_count": 0,
                "status": "deduplicated",
                "ingestion_job_id": None,
            }

        # Chunk up front regardless of mode. Chunking is cheap Python
        # work; doing it here keeps the document and chunks consistent
        # within one transaction's worth of writes.
        chunks = chunk_text(content, chunk_size=chunk_size, overlap=overlap)
        initial_status = "queued" if async_mode else "running"

        document_id, job_id = await _insert_document_and_chunks(
            conn,
            owner_user_id=owner_user_id,
            project_id=project_id,
            title=title,
            content=content,
            source_uri=source_uri,
            content_type=content_type,
            sha=sha,
            size_bytes=size_bytes,
            metadata=meta_json,
            chunks=chunks,
            job_status=initial_status,
        )

    if async_mode:
        return {
            "document_id": str(document_id),
            "sha256": sha,
            "chunk_count": len(chunks),
            "status": "queued",
            "ingestion_job_id": str(job_id),
        }

    # Sync: embed inline. _embed_chunks_for_job handles the failure
    # bookkeeping (marks job failed, re-raises) so we just let the
    # exception propagate.
    await _embed_chunks_for_job(pool, owner_user_id, job_id)

    return {
        "document_id": str(document_id),
        "sha256": sha,
        "chunk_count": len(chunks),
        "status": "complete",
        "ingestion_job_id": str(job_id),
    }


async def ingest_file(
    owner_user_id: UUID,
    project_id: UUID,
    path: str,
    title: Optional[str] = None,
    content_type: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    async_mode: bool = False,
) -> Dict[str, Any]:
    """
    Read a plain-text file from disk and ingest it.

    Infers title from the basename and content_type from the file
    extension when not provided. Binary formats (PDF, docx, etc.) are
    out of scope for Phase A.

    ``async_mode`` is forwarded to ingest_text.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        IsADirectoryError: If ``path`` points at a directory.
        UnicodeDecodeError: If the file is not valid UTF-8.
    """
    if not path:
        raise ValueError("path is required")

    resolved = os.path.abspath(path)
    if not os.path.exists(resolved):
        raise FileNotFoundError(resolved)
    if os.path.isdir(resolved):
        raise IsADirectoryError(resolved)

    with open(resolved, "r", encoding="utf-8") as f:
        content = f.read()

    inferred_title = title or os.path.basename(resolved)
    inferred_content_type = (
        content_type
        or _EXTENSION_CONTENT_TYPES.get(os.path.splitext(resolved)[1].lower(),
                                        "text/plain")
    )

    return await ingest_text(
        owner_user_id=owner_user_id,
        project_id=project_id,
        title=inferred_title,
        content=content,
        source_uri=resolved,
        content_type=inferred_content_type,
        metadata=metadata,
        chunk_size=chunk_size,
        overlap=overlap,
        async_mode=async_mode,
    )
