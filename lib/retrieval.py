"""
Composed retrieval across memories and doc_chunks.

Phase C of the RAG integration. The individual tools memory_search
and rag_search each return one kind of hit; compose_recall fuses
both streams into a single ranked list so a caller that just wants
"relevant stuff about X" does not need to pick a source.

Fusion is Reciprocal Rank Fusion (RRF) on the already-ranked
per-source lists:

    recall_score(row) = 1 / (k + rank_within_its_source_list)

with k=60. A memory hit at rank 1 of the memory list ties with a
doc chunk at rank 1 of the doc list (both score 1/61), and the
tiebreaker preserves the underlying per-source ordering. In
practice a union of top hits from both sides is what callers get.

Both per-source queries default to hybrid=True so exact-keyword
matches get their fair share alongside vector semantic matches.

The unified result shape keeps ``kind: "memory"|"doc"`` on every
row plus common fields (id, content, scope, project, recall_score).
Kind-specific fields (importance / document_title / char offsets /
etc.) are preserved inline so callers can route without re-querying.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence
from uuid import UUID


_RRF_K = 60
# Per-source candidate pool. Bigger than the final limit so RRF has
# room to reorder; without this, the final top-K is just an
# interleave of the per-source top-K and RRF adds no value.
_PER_SOURCE_MULTIPLIER = 2
_MIN_PER_SOURCE = 10

KIND_MEMORY = "memory"
KIND_DOC = "doc"
_ALL_KINDS = (KIND_MEMORY, KIND_DOC)


def _normalize_kinds(kinds: Optional[Sequence[str]]) -> List[str]:
    """Resolve the ``kinds`` argument to a concrete list. None or an
    empty value means "all kinds." Unknown values are dropped rather
    than raising so a caller with a typo still gets results."""
    if not kinds:
        return list(_ALL_KINDS)
    out = [k for k in kinds if k in _ALL_KINDS]
    if not out:
        return list(_ALL_KINDS)
    # Preserve caller-supplied order in case they care about it for
    # tie-breaking (same recall_score -> caller's first kind wins).
    return out


def _memory_to_recall_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a search_memories result into the unified recall shape."""
    unified = {
        "kind": KIND_MEMORY,
        "id": row["id"],
        "content": row.get("content"),
        "scope": row.get("scope"),
        "project": row.get("project"),
    }
    # Preserve memory-specific extras inline.
    for field in (
        "tags", "importance", "temperature",
        "created_at", "last_accessed", "access_count",
        "similarity", "text_score", "rrf_score",
    ):
        if field in row:
            unified[field] = row[field]
    return unified


def _doc_to_recall_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a search_docs chunk result into the unified recall shape.

    The chunk's own id is stored under ``chunk_id`` in search_docs output;
    we surface both ``id`` (= chunk_id, to match the memory shape) and
    ``chunk_id`` (for callers that already know the rag_search field
    layout). document_id / document_title / source offsets stay as-is.
    """
    unified = {
        "kind": KIND_DOC,
        "id": row["chunk_id"],
        "content": row.get("content"),
        "scope": row.get("scope"),
        "project": row.get("project"),
    }
    for field in (
        "chunk_id", "document_id", "document_title",
        "source_uri", "content_type",
        "chunk_index", "char_start", "char_end",
        "similarity", "text_score", "rrf_score",
    ):
        if field in row:
            unified[field] = row[field]
    return unified


def _fuse(
    memory_rows: List[Dict[str, Any]],
    doc_rows: List[Dict[str, Any]],
    limit: int,
) -> List[Dict[str, Any]]:
    """
    Reciprocal Rank Fusion across two already-ranked lists. Each row
    contributes once (it belongs to one source), so the fused score
    is just 1 / (k + rank_in_its_source_list).

    Tiebreaking: stable. When two rows have identical recall_score
    (always the case for same-rank rows from different sources),
    whichever appears first in the combined iteration wins. We iterate
    memory first so a memory hit and a doc hit at the same rank land
    with the memory first, which is the typical caller preference
    (curated content beats raw doc chunk when the fused score ties).
    """
    fused: List[Dict[str, Any]] = []
    for idx, row in enumerate(memory_rows):
        unified = _memory_to_recall_row(row)
        unified["recall_score"] = round(1.0 / (_RRF_K + idx + 1), 6)
        fused.append(unified)
    for idx, row in enumerate(doc_rows):
        unified = _doc_to_recall_row(row)
        unified["recall_score"] = round(1.0 / (_RRF_K + idx + 1), 6)
        fused.append(unified)

    # Python sort is stable, so equal recall_scores retain their
    # insertion order (memories first at any given rank position).
    fused.sort(key=lambda r: r["recall_score"], reverse=True)
    return fused[:limit]


async def compose_recall(
    owner_user_id: UUID,
    project_id: UUID,
    query: str,
    limit: int = 10,
    *,
    kinds: Optional[Sequence[str]] = None,
    hybrid: bool = True,
) -> List[Dict[str, Any]]:
    """
    Unified retrieval across memories and doc_chunks.

    Fires the selected kinds in parallel (well, sequentially in v1;
    asyncio.gather is a two-line change later if latency matters),
    then fuses via RRF. Returns a single list ranked best-first with
    ``kind`` on every row so callers can route.

    Args:
        owner_user_id: Caller identity. Required.
        project_id: Primary project. Scope expansion applies in each
            per-source search via get_visible_project_ids, so the
            caller's globals and declared domains surface too.
        query: Natural-language query.
        limit: Final result count. Per-source candidate pool is
            2 * limit (min 10) so RRF has room to rerank.
        kinds: Which sources to query. None / empty -> both.
        hybrid: Forwarded to search_memories and search_docs. Default
            True, because composed retrieval is the one place where
            keyword + vector fusion carries its weight.

    Returns:
        List of unified rows sorted by recall_score DESC.
    """
    if not query or not query.strip():
        return []
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100

    resolved_kinds = _normalize_kinds(kinds)
    per_source = max(_MIN_PER_SOURCE, limit * _PER_SOURCE_MULTIPLIER)

    memory_rows: List[Dict[str, Any]] = []
    doc_rows: List[Dict[str, Any]] = []

    if KIND_MEMORY in resolved_kinds:
        from lib.db import search_memories
        from lib.embeddings import embed
        query_embedding = embed(query)
        memory_rows = await search_memories(
            query_embedding=query_embedding,
            project_id=project_id,
            owner_user_id=owner_user_id,
            limit=per_source,
            hybrid=hybrid,
            query_text=query,
        )

    if KIND_DOC in resolved_kinds:
        from lib.rag.search import search_docs
        doc_rows = await search_docs(
            owner_user_id=owner_user_id,
            project_id=project_id,
            query=query,
            limit=per_source,
            hybrid=hybrid,
        )

    return _fuse(memory_rows, doc_rows, limit)
