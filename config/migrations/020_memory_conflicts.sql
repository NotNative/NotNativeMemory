-- Migration 020: Disagreement-aware consolidation
--
-- Tracks detected conflicts between memories that are semantically
-- close but potentially contradictory. Preserves both sides rather
-- than silently merging or overwriting.

CREATE TABLE IF NOT EXISTS memory_conflicts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    memory_id_a UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    memory_id_b UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    similarity FLOAT NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ,
    resolution TEXT
        CHECK (resolution IS NULL OR resolution IN ('keep_both', 'supersede_a', 'supersede_b', 'merged', 'dismissed')),
    owner_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    CONSTRAINT memory_conflicts_pair_unique UNIQUE (memory_id_a, memory_id_b)
);

CREATE INDEX IF NOT EXISTS idx_memory_conflicts_unresolved
    ON memory_conflicts (owner_user_id, resolved_at)
    WHERE resolved_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_memory_conflicts_memory_a
    ON memory_conflicts (memory_id_a);

CREATE INDEX IF NOT EXISTS idx_memory_conflicts_memory_b
    ON memory_conflicts (memory_id_b);

-- RLS policy
DROP POLICY IF EXISTS memory_conflicts_owner_rls ON memory_conflicts;
CREATE POLICY memory_conflicts_owner_rls ON memory_conflicts
    USING (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    )
    WITH CHECK (
        current_setting('app.current_user', true) = 'admin'
        OR owner_user_id::text = current_setting('app.current_user', true)
    );

ALTER TABLE memory_conflicts ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_conflicts FORCE ROW LEVEL SECURITY;
