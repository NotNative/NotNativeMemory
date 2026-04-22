"""
Ingestion pipeline for RAG documents.

Synchronous/inline in v1: the caller blocks until the entire
chunk-and-embed pass finishes. The ingestion_jobs row exists so a
future async worker can pick the same shape up without touching the
schema.

Dedup is per owner via sha256 over the raw content. Re-ingesting the
same bytes is a no-op that returns the existing document_id; callers
who want to force a re-embed delete the document first.

Error handling: any failure between INSERT documents and the final
ingestion_jobs update marks the job `failed` with the exception text
and re-raises, so the caller (tool handler) can surface the error via
the standard _tool_error channel.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, List, Optional
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
) -> Dict[str, Any]:
    """
    Ingest a text blob into documents + doc_chunks.

    Dedup: if (owner_user_id, sha256) already exists, returns the
    existing document_id with status='deduplicated' and no chunk work
    done. Otherwise inserts a new documents row, chunks, embeds, and
    stores.

    Args:
        owner_user_id: Caller identity. Required for RLS + dedup scope.
        project_id: Target project. Resolved by the caller via
            get_or_create_project on the scope directory.
        title: Human-readable display name for the document.
        content: The full document text (UTF-8 string).
        source_uri: Where the content came from (file path, URL, or None
            for pasted text). Stored verbatim; not interpreted.
        content_type: MIME hint for the payload. Default text/plain.
        metadata: Arbitrary JSON-serializable dict saved on the
            document row. Useful for authors, ingestion version, tags.
        chunk_size, overlap: Passed through to chunk_text.

    Returns:
        Dict with document_id, sha256, chunk_count, status, and the
        ingestion_job_id so callers can surface status later if needed.

    Raises:
        ValueError: If content is empty or title is empty.
        Any exception from the embedder or DB: re-raised after marking
        the ingestion_job row `failed`.
    """
    if not content or not content.strip():
        raise ValueError("content cannot be empty")
    if not title or not title.strip():
        raise ValueError("title cannot be empty")

    from lib import rls
    from lib.db import get_pool
    from lib.embeddings import embed_batch

    sha = _sha256_of(content)
    size_bytes = len(content.encode("utf-8"))
    meta_json = metadata or {}

    import json

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

        # Insert the document row up front so the ingestion_job has a
        # valid FK. No chunks yet.
        doc_row = await conn.fetchrow(
            """
            INSERT INTO documents
                (project_id, owner_user_id, title, source_uri,
                 content_type, sha256, size_bytes, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
            RETURNING id
            """,
            project_id, owner_user_id, title, source_uri,
            content_type, sha, size_bytes, json.dumps(meta_json),
        )
        document_id = doc_row["id"]

        job_row = await conn.fetchrow(
            """
            INSERT INTO ingestion_jobs
                (document_id, owner_user_id, status, started_at)
            VALUES ($1, $2, 'running', now())
            RETURNING id
            """,
            document_id, owner_user_id,
        )
        job_id = job_row["id"]

    # Chunk + embed + insert outside the RLS connection so we do not
    # hold a pool slot through a potentially slow embedding pass.
    try:
        chunks = chunk_text(content, chunk_size=chunk_size, overlap=overlap)
        if not chunks:
            # content was all whitespace; nothing to embed
            vectors: List[List[float]] = []
        else:
            vectors = embed_batch([c[1] for c in chunks])

        async with rls.app_conn(pool, owner_user_id) as conn:
            # Batched insert of all chunk rows in one round trip.
            if chunks:
                rows_to_insert = [
                    (
                        document_id,
                        project_id,
                        owner_user_id,
                        idx,
                        text,
                        str(vec),  # pgvector accepts the string form
                        start,
                        end,
                        "{}",
                    )
                    for (idx, text, start, end), vec in zip(chunks, vectors)
                ]
                await conn.executemany(
                    """
                    INSERT INTO doc_chunks
                        (document_id, project_id, owner_user_id, chunk_index,
                         content, embedding, char_start, char_end, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6::vector, $7, $8, $9::jsonb)
                    """,
                    rows_to_insert,
                )

            await conn.execute(
                """
                UPDATE ingestion_jobs
                   SET status = 'complete',
                       chunk_count = $2,
                       finished_at = now()
                 WHERE id = $1
                """,
                job_id, len(chunks),
            )

    except Exception as exc:
        # Mark the job failed so the failure is observable. Swallow any
        # secondary failure from the UPDATE itself so the original
        # exception propagates cleanly.
        try:
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
        except Exception:
            pass
        raise

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
) -> Dict[str, Any]:
    """
    Read a plain-text file from disk and ingest it.

    Infers title from the basename and content_type from the file
    extension when not provided. Binary formats (PDF, docx, etc.) are
    out of scope for Phase A.

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
    )
