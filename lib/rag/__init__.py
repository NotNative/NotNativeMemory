"""
RAG scaffolding for NotNativeMemory.

Separate from the memories stack so that bulk-ingested documents do not
share the curated memory's lifecycle (importance, thermal decay, cap
eviction, dedup). Documents are stored in `documents`, chunked into
`doc_chunks` with pgvector embeddings sized to match memories
(vector(1024)), and tracked by `ingestion_jobs` for inline-or-future-
async ingestion.

Public entry points are exposed via the three rag_* MCP tools in
server.py. Everything in this package is called from those handlers
or from tests.
"""
