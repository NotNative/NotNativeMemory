"""
Semantic search over doc_chunks.

Mirrors the memory_search scope expansion: a local project sees its
own chunks plus chunks in the caller's _global and declared _domain_*
projects. Global and domain projects see only themselves.

Results are ranked by raw cosine similarity (no importance weighting —
doc chunks are uniform, unlike memories where critical > normal). The
ivfflat index on doc_chunks.embedding handles the ordering; callers
get a flat list sorted best-first.

Returned rows carry enough context (document title + source_uri +
chunk_index) for an agent to cite the hit back to the source and,
eventually, to fetch adjacent chunks for window expansion.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import UUID


_MAX_LIMIT = 100
_MIN_LIMIT = 1


def _format_chunk_row(row: Any) -> Dict[str, Any]:
    """Shape a doc_chunks JOIN documents+projects row into the tool
    response dict. Keeping this in one place so the three access paths
    (tool handler, test helper, future admin view) stay consistent."""
    out: Dict[str, Any] = {
        "chunk_id": str(row["chunk_id"]),
        "document_id": str(row["document_id"]),
        "document_title": row["document_title"],
        "source_uri": row["document_source"],
        "content_type": row["content_type"],
        "chunk_index": row["chunk_index"],
        "char_start": row["char_start"],
        "char_end": row["char_end"],
        "content": row["content"],
    }
    if "similarity" in row.keys():
        out["similarity"] = round(float(row["similarity"]), 4)
    if "project_scope" in row.keys():
        out["scope"] = row["project_scope"]
    if "project_name" in row.keys():
        out["project"] = row["project_name"]
    return out


async def search_docs(
    owner_user_id: UUID,
    project_id: UUID,
    query: str,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """
    Return the top-``limit`` chunks matching ``query`` under the
    caller's visible project set.

    Args:
        owner_user_id: Caller identity. Every returned row has this
            owner (enforced by the WHERE and by RLS).
        project_id: Primary project. Scope expansion joins in the
            caller's globals and matching domains via
            get_visible_project_ids.
        query: Natural-language text. Embedded with the same model
            that ingested the chunks.
        limit: Clamped to [1, 100].

    Returns:
        List of chunk dicts ranked best-first, empty list on no hits.
    """
    if not query or not query.strip():
        return []

    if limit < _MIN_LIMIT:
        limit = _MIN_LIMIT
    if limit > _MAX_LIMIT:
        limit = _MAX_LIMIT

    from lib import rls
    from lib.db import get_pool, get_visible_project_ids
    from lib.embeddings import embed

    query_embedding = embed(query)
    visible_ids = await get_visible_project_ids(project_id, owner_user_id)
    if not visible_ids:
        return []

    pool = await get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        rows = await conn.fetch(
            """
            SELECT
                dc.id            AS chunk_id,
                dc.document_id   AS document_id,
                dc.chunk_index   AS chunk_index,
                dc.content       AS content,
                dc.char_start    AS char_start,
                dc.char_end      AS char_end,
                d.title          AS document_title,
                d.source_uri     AS document_source,
                d.content_type   AS content_type,
                p.scope          AS project_scope,
                p.name           AS project_name,
                1 - (dc.embedding <=> $1::vector) AS similarity
            FROM doc_chunks dc
            JOIN documents  d ON d.id = dc.document_id
            JOIN projects   p ON p.id = dc.project_id
            WHERE dc.project_id = ANY($2)
              AND dc.owner_user_id = $3
              AND dc.embedding IS NOT NULL
            ORDER BY dc.embedding <=> $1::vector ASC
            LIMIT $4
            """,
            str(query_embedding), visible_ids, owner_user_id, limit,
        )

    return [_format_chunk_row(r) for r in rows]


async def get_document_status(
    owner_user_id: UUID, document_id: UUID,
) -> Optional[Dict[str, Any]]:
    """
    Inspect the most recent ingestion_job for a document. Useful for
    surfacing 'failed' ingestions in the tool response without having
    to re-run the pipeline.

    Returns None if the document does not exist or belongs to someone
    else (RLS filters it out either way).
    """
    from lib import rls
    from lib.db import get_pool

    pool = await get_pool()
    async with rls.app_conn(pool, owner_user_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT d.id, d.title, d.sha256, d.size_bytes, d.created_at,
                   j.status, j.chunk_count, j.error, j.finished_at
              FROM documents d
              LEFT JOIN LATERAL (
                  SELECT status, chunk_count, error, finished_at
                    FROM ingestion_jobs
                   WHERE document_id = d.id
                   ORDER BY created_at DESC
                   LIMIT 1
              ) j ON TRUE
             WHERE d.id = $1 AND d.owner_user_id = $2
            """,
            document_id, owner_user_id,
        )

    if not row:
        return None

    return {
        "document_id": str(row["id"]),
        "title": row["title"],
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "created_at": row["created_at"].isoformat(),
        "ingestion_status": row["status"],
        "chunk_count": row["chunk_count"] or 0,
        "error": row["error"],
        "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
    }
