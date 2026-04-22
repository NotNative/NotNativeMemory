"""
Integration tests for compose_recall (unified memory + RAG retrieval).

Covers:
- Seeds one memory with a unique phrase and one RAG document with a
  different unique phrase. Queries for each phrase and asserts:
    - kind="memory" row surfaces for the memory-anchored query
    - kind="doc" row surfaces for the doc-anchored query
    - Unified query returns both kinds
- kinds filter: ["memory"] returns only memory rows; ["doc"] only doc.
- Every returned row carries recall_score, kind, and the required
  common fields (id, content).
- RLS: a second user cannot see the first user's rows via recall.
- Empty query returns []; does not raise.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD  (live pgvector)
    MEMORY_MODEL_PATH                            (loaded)

Usage:
    python tests/test_recall.py
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


# Two distinct sentinel tokens so we can tell a memory hit from a
# doc hit without inspecting content structure.
MEMORY_TOKEN = "recallmemsentinel"
DOC_TOKEN = "recalldocsentinel"


async def run() -> int:
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id
    from lib.embeddings import embed
    from lib.rag.ingest import ingest_text
    from lib.retrieval import compose_recall, KIND_MEMORY, KIND_DOC

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
    alice_name = f"recall-alice-{run_id}"
    bob_name = f"recall-bob-{run_id}"

    pool = await db.get_pool()
    alice = await auth_db.create_user(alice_name, "password-1234")
    alice_uid = UUID(alice["id"])
    bob = await auth_db.create_user(bob_name, "password-1234")
    bob_uid = UUID(bob["id"])

    set_current_user_id(alice_uid)
    project_dir = f"/tmp/recall-{run_id}"
    project_id = await db.get_or_create_project(project_dir, owner_user_id=alice_uid)

    # Seed memory with unique token.
    memory_content = (
        f"Key engineering decision tagged with {MEMORY_TOKEN}. The team "
        f"agreed {MEMORY_TOKEN} is the canonical reference marker."
    )
    memory_id = await db.store_memory(
        content=memory_content,
        embedding=embed(memory_content),
        project_id=project_id,
        owner_user_id=alice_uid,
        importance="normal",
    )

    # Seed RAG doc with different unique token.
    doc_content = (
        f"Reference document discussing {DOC_TOKEN} in operational "
        f"context. {DOC_TOKEN} appears in multiple paragraphs as a "
        f"load-bearing identifier for downstream indexing."
    )
    doc_result = await ingest_text(
        owner_user_id=alice_uid,
        project_id=project_id,
        title="recall-test-doc",
        content=doc_content,
    )
    doc_id = doc_result["document_id"]

    # 1. Query with memory-anchored token: memory row should surface
    # and carry kind=memory.
    mem_query_results = await compose_recall(
        owner_user_id=alice_uid,
        project_id=project_id,
        query=MEMORY_TOKEN,
        limit=5,
    )
    check("memory-token query returns at least one row",
          len(mem_query_results) >= 1)
    mem_hit = next(
        (r for r in mem_query_results if r.get("id") == str(memory_id)),
        None,
    )
    check("memory-token query surfaces the seeded memory", mem_hit is not None)
    if mem_hit is not None:
        check("surfaced memory row is tagged kind='memory'",
              mem_hit.get("kind") == KIND_MEMORY)
        check("surfaced memory row has recall_score > 0",
              (mem_hit.get("recall_score") or 0) > 0)
        check("surfaced memory row carries importance (memory extra)",
              mem_hit.get("importance") == "normal")

    # 2. Query with doc-anchored token: doc chunk should surface with
    # kind=doc, document_title + source metadata present.
    doc_query_results = await compose_recall(
        owner_user_id=alice_uid,
        project_id=project_id,
        query=DOC_TOKEN,
        limit=5,
    )
    check("doc-token query returns at least one row",
          len(doc_query_results) >= 1)
    doc_hit = next(
        (r for r in doc_query_results if r.get("document_id") == doc_id),
        None,
    )
    check("doc-token query surfaces the seeded doc chunk",
          doc_hit is not None)
    if doc_hit is not None:
        check("surfaced doc row is tagged kind='doc'",
              doc_hit.get("kind") == KIND_DOC)
        check("surfaced doc row carries document_title",
              doc_hit.get("document_title") == "recall-test-doc")
        check("surfaced doc row carries recall_score > 0",
              (doc_hit.get("recall_score") or 0) > 0)

    # 3. kinds filter: ["memory"] excludes doc rows even when query
    # matches a doc.
    mem_only = await compose_recall(
        owner_user_id=alice_uid,
        project_id=project_id,
        query=DOC_TOKEN,  # matches doc, should not surface
        limit=5,
        kinds=["memory"],
    )
    check("kinds=['memory'] returns only memory rows (or empty)",
          all(r.get("kind") == KIND_MEMORY for r in mem_only))

    # 4. kinds filter: ["doc"] excludes memory rows.
    doc_only = await compose_recall(
        owner_user_id=alice_uid,
        project_id=project_id,
        query=MEMORY_TOKEN,  # matches memory, should not surface
        limit=5,
        kinds=["doc"],
    )
    check("kinds=['doc'] returns only doc rows (or empty)",
          all(r.get("kind") == KIND_DOC for r in doc_only))

    # 5. Both kinds present in the result when the query matches
    # across types. Use a generic keyword both seeds contain to hit
    # both sides, but prefer an empty-phrase-free approach: combine
    # both sentinels in one query string.
    unified = await compose_recall(
        owner_user_id=alice_uid,
        project_id=project_id,
        query=f"{MEMORY_TOKEN} {DOC_TOKEN}",
        limit=10,
    )
    kinds_seen = {r.get("kind") for r in unified}
    check("unified query surfaces both kinds",
          KIND_MEMORY in kinds_seen and KIND_DOC in kinds_seen)

    # Every row should have the common field set.
    check("every row has id, content, kind, recall_score",
          all(
              {"id", "content", "kind", "recall_score"}.issubset(r.keys())
              for r in unified
          ))

    # 6. Empty query returns [] and does not raise.
    empty = await compose_recall(
        owner_user_id=alice_uid,
        project_id=project_id,
        query="   ",
        limit=5,
    )
    check("empty query returns empty list", empty == [])

    # 7. RLS: bob's recall call does not see alice's rows.
    set_current_user_id(bob_uid)
    bob_project_id = await db.get_or_create_project(
        f"/tmp/recall-bob-{run_id}", owner_user_id=bob_uid,
    )
    bob_results = await compose_recall(
        owner_user_id=bob_uid,
        project_id=bob_project_id,
        query=f"{MEMORY_TOKEN} {DOC_TOKEN}",
        limit=10,
    )
    check("cross-user recall does not see alice's memory",
          all(r.get("id") != str(memory_id) for r in bob_results))
    check("cross-user recall does not see alice's doc chunks",
          all(r.get("document_id") != doc_id for r in bob_results))

    # Cleanup.
    async with rls.admin_conn(pool) as conn:
        await conn.execute("DELETE FROM users WHERE id = ANY($1)",
                           [alice_uid, bob_uid])
    await db.close_pool()

    print("---")
    print(f"{total - failed}/{total} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
