-- Migration 002: Thermal decay and usage tracking
-- Adds temperature-based memory lifecycle and stats collection
-- for future self-tuning decay rates.

-- Temperature column: memories cool over time, reheat on access.
-- Critical importance memories never decay (lex aeterna equivalent).
ALTER TABLE memories ADD COLUMN IF NOT EXISTS temperature FLOAT DEFAULT 70.0;

-- Index for eviction queries (find coldest memories)
CREATE INDEX IF NOT EXISTS idx_memories_temperature
    ON memories (project_id, temperature ASC);

-- Decay stats: tracks usage patterns per project for future self-tuning.
-- Each row is a snapshot of one decay cycle's observations.
CREATE TABLE IF NOT EXISTS decay_stats (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
    recorded_at TIMESTAMPTZ DEFAULT now(),

    -- Snapshot metrics
    total_memories INT NOT NULL DEFAULT 0,
    memories_accessed_since_last INT NOT NULL DEFAULT 0,
    memories_decayed INT NOT NULL DEFAULT 0,
    memories_evicted INT NOT NULL DEFAULT 0,
    memories_deduplicated INT NOT NULL DEFAULT 0,

    -- Derived metrics (computed at record time)
    avg_temperature FLOAT,
    avg_access_count FLOAT,
    median_access_interval_hours FLOAT
);

-- Index for querying recent stats per project
CREATE INDEX IF NOT EXISTS idx_decay_stats_project_time
    ON decay_stats (project_id, recorded_at DESC);

-- Cap decay_stats to prevent unbounded growth (keep 90 days)
-- Cleaned up by passive maintenance alongside memory decay.
