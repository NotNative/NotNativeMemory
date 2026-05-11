"""
Tests for lib.rag.search.list_documents.

Verifies:
  - returns ingested docs newest-first
  - limit/offset pagination works and is stable
  - chunk_count matches doc_chunks rowcount
  - cross-user isolation: B does not see A's docs
  - empty project returns empty list

Standalone async script, same pattern as test_rag_roundtrip.py.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD
    MEMORY_MODEL_PATH
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


async def run() -> int:
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id
    from lib.rag.ingest import ingest_text
    from lib.rag.search import list_documents

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

    test_username = f"rag-list-{secrets.token_hex(4)}"
    test_project_dir = f"/tmp/rag-list-{secrets.token_hex(4)}"

    pool = await db.get_pool()

    user = await auth_db.create_user(test_username, "password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)

    project_id = await db.get_or_create_project(test_project_dir, uid)

    # 0. Empty project: list returns empty.
    empty_list = await list_documents(uid, project_id)
    check("list on empty project returns []", empty_list == [])

    # 1. Ingest three docs with distinguishable content so dedup
    #    does not collapse them.
    doc_ids = []
    titles = []
    for i in range(3):
        title = f"list-test doc {i} {secrets.token_hex(2)}"
        content = (
            f"Document number {i} unique body {secrets.token_hex(4)}.\n"
            "Plain prose so the chunker produces at least one chunk."
        )
        res = await ingest_text(
            owner_user_id=uid,
            project_id=project_id,
            title=title,
            content=content,
            source_uri=f"test://list/{i}",
        )
        check(f"ingest #{i} status complete",
              res.get("status") == "complete")
        doc_ids.append(res["document_id"])
        titles.append(title)
        # Small spacing so created_at strictly increases between rows
        # without depending on clock resolution.
        await asyncio.sleep(0.01)

    # 2. Full list.
    all_docs = await list_documents(uid, project_id, limit=50)
    check("list returns all 3 ingested docs", len(all_docs) == 3)

    # 3. Ordering: newest first means last-ingested is index 0.
    if len(all_docs) == 3:
        check("newest doc is first",
              all_docs[0]["document_id"] == doc_ids[2])
        check("oldest doc is last",
              all_docs[2]["document_id"] == doc_ids[0])

        # Required fields present.
        row = all_docs[0]
        for field in ("document_id", "title", "source_uri", "content_type",
                      "size_bytes", "chunk_count", "created_at", "scope",
                      "project"):
            check(f"list row carries '{field}'", field in row)

        check("chunk_count is a positive int",
              isinstance(row["chunk_count"], int) and row["chunk_count"] >= 1)
        check("source_uri preserved verbatim",
              row["source_uri"] == "test://list/2")
        check("scope reports 'local' for a local project",
              row["scope"] == "local")

    # 4. Pagination: limit=2 returns 2, offset=2 returns the third.
    page1 = await list_documents(uid, project_id, limit=2, offset=0)
    page2 = await list_documents(uid, project_id, limit=2, offset=2)
    check("limit=2 page 1 has 2 rows", len(page1) == 2)
    check("limit=2 page 2 has 1 row", len(page2) == 1)
    if page1 and page2:
        page1_ids = {r["document_id"] for r in page1}
        page2_ids = {r["document_id"] for r in page2}
        check("pages are disjoint", page1_ids.isdisjoint(page2_ids))

    # 5. chunk_count agrees with admin-bypass rowcount.
    if all_docs:
        sample = all_docs[0]
        sample_id = UUID(sample["document_id"])
        async with rls.admin_conn(pool) as conn:
            real_count = await conn.fetchval(
                "SELECT COUNT(*) FROM doc_chunks WHERE document_id = $1",
                sample_id,
            )
        check("chunk_count matches doc_chunks rowcount",
              sample["chunk_count"] == real_count)

    # 6. Cross-user isolation.
    other_username = f"rag-list-other-{secrets.token_hex(4)}"
    other = await auth_db.create_user(other_username, "password-1234")
    other_uid = UUID(other["id"])
    other_project_id = await db.get_or_create_project(
        f"/tmp/rag-list-other-{secrets.token_hex(4)}", other_uid,
    )
    other_view = await list_documents(other_uid, other_project_id)
    check("other user sees none of the test docs",
          all(r["document_id"] not in doc_ids for r in other_view))

    # 7. limit clamp: passing 0 clamps to 1, passing 999 clamps to 100.
    one = await list_documents(uid, project_id, limit=0)
    check("limit=0 clamps to >=1 result when rows exist", len(one) >= 1)
    big = await list_documents(uid, project_id, limit=999)
    check("limit=999 returns <= corpus (clamp does not error)",
          len(big) == 3)

    # Cleanup.
    async with rls.admin_conn(pool) as conn:
        for did in doc_ids:
            await conn.execute("DELETE FROM documents WHERE id = $1",
                               UUID(did))
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
