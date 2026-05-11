"""
Tests for lib.rag.search.forget_document.

Verifies:
  - happy path: forget returns True, chunks + ingestion_jobs gone (FK cascade)
  - idempotency: a second forget for the same id returns False
  - cross-user isolation: user B cannot forget user A's document
  - bogus UUID: forget returns False without raising

Standalone async script, same pattern as test_rag_roundtrip.py.

Requires env:
    MEMORY_DB_HOST, PORT, NAME, USER, PASSWORD
    MEMORY_MODEL_PATH
"""

import asyncio
import os
import secrets
import sys
from uuid import UUID, uuid4

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)


DOC_CONTENT = """\
# Forget Test Document

Plain prose for the forget round-trip test. Contents do not matter --
we only care that ingest writes rows and forget cascades them away.
"""


async def run() -> int:
    from lib import auth_db, db, rls
    from lib.auth_context import set_current_user_id
    from lib.rag.ingest import ingest_text
    from lib.rag.search import forget_document

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

    test_username = f"rag-forget-{secrets.token_hex(4)}"
    test_project_dir = f"/tmp/rag-forget-{secrets.token_hex(4)}"

    pool = await db.get_pool()

    user = await auth_db.create_user(test_username, "password-1234")
    uid = UUID(user["id"])
    set_current_user_id(uid)

    project_id = await db.get_or_create_project(test_project_dir, uid)

    # 1. Ingest a doc.
    result = await ingest_text(
        owner_user_id=uid,
        project_id=project_id,
        title="forget test doc",
        content=DOC_CONTENT,
        source_uri=None,
    )
    check("ingest succeeded",
          result.get("status") == "complete"
          and bool(result.get("document_id")))
    document_id = UUID(result["document_id"])

    async with rls.admin_conn(pool) as conn:
        chunk_count = await conn.fetchval(
            "SELECT COUNT(*) FROM doc_chunks WHERE document_id = $1",
            document_id,
        )
        job_count = await conn.fetchval(
            "SELECT COUNT(*) FROM ingestion_jobs WHERE document_id = $1",
            document_id,
        )
    check("chunks exist before forget", chunk_count >= 1)
    check("ingestion_job row exists before forget", job_count >= 1)

    # 2. Forget returns True.
    forgotten = await forget_document(uid, document_id)
    check("forget_document returns True on owner delete", forgotten is True)

    # 3. Cascade: chunks + jobs gone, documents row gone.
    async with rls.admin_conn(pool) as conn:
        doc_after = await conn.fetchval(
            "SELECT COUNT(*) FROM documents WHERE id = $1", document_id,
        )
        chunks_after = await conn.fetchval(
            "SELECT COUNT(*) FROM doc_chunks WHERE document_id = $1",
            document_id,
        )
        jobs_after = await conn.fetchval(
            "SELECT COUNT(*) FROM ingestion_jobs WHERE document_id = $1",
            document_id,
        )
    check("documents row gone after forget", doc_after == 0)
    check("doc_chunks rows cascaded away", chunks_after == 0)
    check("ingestion_jobs rows cascaded away", jobs_after == 0)

    # 4. Idempotent: second forget of the same id returns False.
    forgotten_again = await forget_document(uid, document_id)
    check("second forget returns False", forgotten_again is False)

    # 5. Bogus UUID that never existed: returns False, does not raise.
    bogus = uuid4()
    bogus_result = await forget_document(uid, bogus)
    check("forgetting a non-existent UUID returns False",
          bogus_result is False)

    # 6. Cross-user isolation. Ingest as user B, try to forget as user A.
    other_username = f"rag-forget-other-{secrets.token_hex(4)}"
    other = await auth_db.create_user(other_username, "password-1234")
    other_uid = UUID(other["id"])
    other_project_id = await db.get_or_create_project(
        f"/tmp/rag-forget-other-{secrets.token_hex(4)}", other_uid,
    )
    other_result = await ingest_text(
        owner_user_id=other_uid,
        project_id=other_project_id,
        title="other user's doc",
        content=DOC_CONTENT,
        source_uri=None,
    )
    other_doc_id = UUID(other_result["document_id"])

    cross = await forget_document(uid, other_doc_id)
    check("cross-user forget returns False (no theft)", cross is False)

    async with rls.admin_conn(pool) as conn:
        other_still_there = await conn.fetchval(
            "SELECT COUNT(*) FROM documents WHERE id = $1", other_doc_id,
        )
    check("other user's document still present after cross-user attempt",
          other_still_there == 1)

    # Owner B can still delete their own.
    forgotten_by_owner = await forget_document(other_uid, other_doc_id)
    check("owner can delete their own document", forgotten_by_owner is True)

    # Cleanup
    async with rls.admin_conn(pool) as conn:
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
