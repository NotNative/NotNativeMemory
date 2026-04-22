"""
Character-based sliding-window chunker for RAG ingestion.

v1 is intentionally simple: fixed-size windows with a fixed overlap,
slicing on character boundaries with no sentence/paragraph awareness.
Good enough for prose and markdown where the embedder's context window
is forgiving; not tuned for code or structured documents.

Tokens vs. characters: we count characters, not tokens, to keep the
chunker free of the embedding model dependency. 2000 characters maps
to roughly 450-550 English tokens, well under the gte-large 8192 max.
Callers who need tighter control pass explicit chunk_size / overlap.

Returned tuples are (chunk_index, content, char_start, char_end) where
char_start is inclusive and char_end is exclusive, i.e. the slice is
`text[char_start:char_end]`. The ordering matches what doc_chunks.
chunk_index expects so a caller can drop straight into an INSERT.
"""

from __future__ import annotations

from typing import List, Tuple

DEFAULT_CHUNK_SIZE = 2000
DEFAULT_OVERLAP = 250


def chunk_text(
    content: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> List[Tuple[int, str, int, int]]:
    """
    Slice ``content`` into overlapping chunks.

    Args:
        content: The full document text. Empty string yields no chunks.
        chunk_size: Maximum characters per chunk. Must be > 0.
        overlap: Characters each chunk shares with the previous one.
            Must be < chunk_size so the window always advances.

    Returns:
        List of (chunk_index, chunk_content, char_start, char_end).
        An empty list when ``content`` is empty.

    Raises:
        ValueError: If chunk_size <= 0 or overlap >= chunk_size.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if overlap < 0:
        raise ValueError("overlap cannot be negative")
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size")

    if not content:
        return []

    step = chunk_size - overlap
    chunks: List[Tuple[int, str, int, int]] = []

    n = len(content)
    idx = 0
    start = 0
    while start < n:
        end = min(start + chunk_size, n)
        chunks.append((idx, content[start:end], start, end))
        if end >= n:
            break
        start += step
        idx += 1

    return chunks
