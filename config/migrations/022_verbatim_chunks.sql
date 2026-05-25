-- Migration 022: verbatim_chunks — primary-source transcript storage
--
-- Adds the "drawers" layer of a mempalace-style two-layer memory stack.
-- The existing `memories` table is the "closets" layer: curated, dedup'd,
-- thermal, capped. This new table is the raw transcript: append-only,
-- per-turn chunks, no dedup, no cooling, no cap. Search uses the same
-- vector(1024) + tsvector + Reciprocal Rank Fusion pattern that memories
-- already use, but lives entirely in its own table so verbatim writes
-- never trigger memories-side dedup/conflict/displacement machinery and
-- verbatim hits never pollute memory_search rankings.
--
-- Replaces the v1 JSONL substring path in lib/verbatim.py, which scanned
-- ~/.nna/transcripts/<session-id>.jsonl on disk and is unreachable when
-- NNM runs on a separate host from the NNA client.
--
-- Label model (v2 §3 of docs/planning/self-improving-ecosystem-v2.md):
--   session_id    — the NNA session that produced the chunk
--   chunk_index   — monotonic per-session order
--   topic         — coarse subject tag, optional (curator-stamped)
--   agent         — which agent emitted (main, subagent name)
--   source_event  — turn.post / tool.call.post / user.prompt.submit / ...
--   is_error      — whether the emitting event was an error/failure
--   loaded_skills — skill names active at capture time
--   mission_id    — NNO mission this turn belongs to, if any
--   mission_type  — coarse mission category
--   outcome       — stamped later via verbatim_stamp_outcome
--                   (e.g. 'success' | 'failure' | 'aborted'); NULL until known
--
-- Idempotent capture: UNIQUE(owner_user_id, session_id, chunk_index) lets
-- the NNA-side hook safely re-send a chunk it isn't sure landed without
-- duplicating; the ON CONFLICT path is owned by the store helper.
--
-- RLS: same admin-sentinel + owner_user_id policy as migration 015's
-- RAG tables. ENABLE + FORCE so the app role gets the same enforcement.
--
-- Rollback: config/migrations/rollback/022_verbatim_chunks.sql.

BEGIN;

CREATE TABLE IF NOT EXISTS verbatim_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,

    session_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,

    content TEXT NOT NULL,
    embedding vector(1024),
    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,

    -- Labels (v2 §3). All optional except source_event.
    topic TEXT,
    agent TEXT,
    source_event TEXT NOT NULL,
    is_error BOOLEAN NOT NULL DEFAULT FALSE,
    loaded_skills TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    mission_id TEXT,
    mission_type TEXT,
    outcome TEXT,

    ts TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent capture key: same (owner, session, index) re-send is a no-op.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_verbatim_chunks_session_index
    ON verbatim_chunks (owner_user_id, session_id, chunk_index);

-- Vector + full-text indexes parallel to memories / doc_chunks.
CREATE INDEX IF NOT EXISTS idx_verbatim_chunks_embedding
    ON verbatim_chunks USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_verbatim_chunks_tsv
    ON verbatim_chunks USING gin (tsv);

-- Common scan patterns.
CREATE INDEX IF NOT EXISTS idx_verbatim_chunks_project_ts
    ON verbatim_chunks (project_id, ts DESC);

CREATE INDEX IF NOT EXISTS idx_verbatim_chunks_owner
    ON verbatim_chunks (owner_user_id);

CREATE INDEX IF NOT EXISTS idx_verbatim_chunks_session
    ON verbatim_chunks (session_id);

CREATE INDEX IF NOT EXISTS idx_verbatim_chunks_mission
    ON verbatim_chunks (mission_id)
    WHERE mission_id IS NOT NULL;

-- Partial index for the outcome-stamping path (cheap scan of unstamped rows).
CREATE INDEX IF NOT EXISTS idx_verbatim_chunks_unstamped
    ON verbatim_chunks (session_id)
    WHERE outcome IS NULL;

-- RLS: admin sentinel + owner match, same shape as RAG tables (mig 015).
DROP POLICY IF EXISTS verbatim_chunks_owner_rls ON verbatim_chunks;
CREATE POLICY verbatim_chunks_owner_rls ON verbatim_chunks
    USING (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    )
    WITH CHECK (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    );

ALTER TABLE verbatim_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE verbatim_chunks FORCE ROW LEVEL SECURITY;

COMMIT;
