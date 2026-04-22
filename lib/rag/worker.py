"""
Background worker for async RAG ingestion.

The ingest pipeline (lib/rag/ingest.py) can insert chunks with NULL
embeddings and leave the ingestion_job in status='queued'. This module
drains the queue.

Two entry points:

- ``process_queued_jobs(pool, limit)`` — claims up to ``limit`` queued
  jobs and calls ``_embed_chunks_for_job`` on each. Returns the number
  processed. Safe to call from anywhere (tests use it directly).

- ``run_worker_loop(pool, stop_event, poll_interval)`` — long-running
  wrapper that does startup recovery and then polls forever. Cancels
  cleanly when ``stop_event`` is set. Started once per HTTP-mode
  server lifecycle.

Job claiming uses FOR UPDATE SKIP LOCKED so multiple worker loops
(different processes, or a future scale-out) never double-claim.
NotNativeMemory is single-instance by design today, but the pattern
costs nothing and future-proofs against it.

Startup recovery: any job left in status='running' at worker start
predates this run (a previous process crashed mid-embed). Flip it
back to 'queued' so the fresh worker picks it up again; the backfill
path is idempotent so chunks already embedded stay embedded.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Tuple
from uuid import UUID

from lib.rag.ingest import _embed_chunks_for_job


_log = logging.getLogger("notnative.rag.worker")


_DEFAULT_POLL_INTERVAL = 2.0
_DEFAULT_BATCH_LIMIT = 5


async def recover_stale_jobs(pool) -> int:
    """
    Flip any ingestion_job stuck in status='running' back to 'queued'.

    Called once at worker startup. The assumption is safe for
    single-instance NNM: if a job is 'running' when a fresh worker
    boots, it must have been orphaned by a prior crash (no other
    worker is concurrently holding it). For multi-instance deployments
    the claim pattern (FOR UPDATE SKIP LOCKED) prevents double-claim
    during normal operation, but startup recovery would need a
    liveness signal to avoid stealing from a live peer.

    Uses admin_conn because recovery touches jobs across all owners.
    Returns the number of rows flipped so operators can see it in
    logs.
    """
    from lib import rls

    async with rls.admin_conn(pool) as conn:
        result = await conn.execute(
            """
            UPDATE ingestion_jobs
               SET status = 'queued',
                   started_at = NULL
             WHERE status = 'running'
            """,
        )
    # asyncpg returns the command tag, e.g. "UPDATE 3".
    try:
        count = int(result.split()[-1])
    except (ValueError, IndexError):
        count = 0
    if count:
        _log.info("recovered %d stale ingestion job(s) to queued", count)
    return count


async def _claim_queued_jobs(pool, limit: int) -> List[Tuple[UUID, UUID]]:
    """
    Atomically transition up to ``limit`` queued jobs to 'running'.

    Returns a list of (job_id, owner_user_id). FOR UPDATE SKIP LOCKED
    guarantees two concurrent workers see disjoint sets even if they
    race on the same queue.
    """
    from lib import rls

    async with rls.admin_conn(pool) as conn:
        rows = await conn.fetch(
            """
            WITH claimed AS (
                SELECT id, owner_user_id
                  FROM ingestion_jobs
                 WHERE status = 'queued'
                 ORDER BY created_at ASC
                 LIMIT $1
                 FOR UPDATE SKIP LOCKED
            )
            UPDATE ingestion_jobs j
               SET status = 'running',
                   started_at = now()
              FROM claimed c
             WHERE j.id = c.id
            RETURNING j.id, j.owner_user_id
            """,
            limit,
        )
    return [(r["id"], r["owner_user_id"]) for r in rows]


async def process_queued_jobs(
    pool, limit: int = _DEFAULT_BATCH_LIMIT,
) -> int:
    """
    Claim up to ``limit`` queued jobs, embed their chunks, and mark
    them complete. Returns the number of jobs handled in this call.

    Each job is processed in isolation: one job's embed failure does
    not block the others in the same batch. ``_embed_chunks_for_job``
    marks failed jobs internally before re-raising, so a raise here
    just means "log and move on" rather than "leave state
    inconsistent."
    """
    claimed = await _claim_queued_jobs(pool, limit)
    if not claimed:
        return 0

    processed = 0
    for job_id, owner_user_id in claimed:
        try:
            await _embed_chunks_for_job(pool, owner_user_id, job_id)
            processed += 1
        except Exception as exc:
            _log.warning(
                "ingestion job %s (owner=%s) failed: %s",
                job_id, owner_user_id, exc,
            )
            # Do not re-raise; other jobs in the batch must still
            # have their chance. _embed_chunks_for_job already marked
            # this one 'failed' in its own handler.
    return processed


async def run_worker_loop(
    pool,
    stop_event: asyncio.Event,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    batch_limit: int = _DEFAULT_BATCH_LIMIT,
) -> None:
    """
    Long-running poll loop. Intended to be spawned once per HTTP-mode
    server process via asyncio.create_task.

    Shuts down cleanly when ``stop_event`` is set; the in-flight batch
    finishes before the loop exits.
    """
    _log.info("rag worker loop starting (poll_interval=%ss)", poll_interval)
    await recover_stale_jobs(pool)

    while not stop_event.is_set():
        try:
            processed = await process_queued_jobs(pool, batch_limit)
        except Exception as exc:
            # Defensive: process_queued_jobs already absorbs per-job
            # failures. A raise here means something broke around the
            # queue itself (e.g. DB disconnect). Log and keep polling
            # so a transient outage does not kill the worker outright.
            _log.exception("rag worker batch errored: %s", exc)
            processed = 0

        if processed == 0:
            # Idle: sleep until poll_interval, or wake early on stop.
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass

    _log.info("rag worker loop stopped")


def start_worker_task(
    pool,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
    batch_limit: int = _DEFAULT_BATCH_LIMIT,
) -> Tuple[asyncio.Task, asyncio.Event]:
    """
    Convenience starter: spawn the worker loop as an asyncio.Task and
    return (task, stop_event). Caller keeps references to both so it
    can signal shutdown and await the task on server stop.
    """
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        run_worker_loop(pool, stop_event, poll_interval, batch_limit),
        name="rag-worker",
    )
    return task, stop_event
