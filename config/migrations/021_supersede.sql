-- Migration 021: User-driven supersede
--
-- Allows explicit marking of a memory as superseded by another.
-- Superseded memories are excluded from search/context results but
-- remain visible in the GUI for audit/history.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS superseded_by UUID REFERENCES memories(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_memories_superseded_by
    ON memories (superseded_by)
    WHERE superseded_by IS NOT NULL;
