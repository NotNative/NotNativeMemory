-- Migration 018: Add memory classification field.
--
-- Adds a nullable `class` column to memories. NULL means unclassified
-- (user hasn't triaged yet). Valid values: rule, preference, memory.
--
-- Rules never decay and never get consolidated. Preferences don't decay
-- but are softer than rules. Memories decay and consolidate normally.
-- The consuming model uses class to decide how strictly to follow the
-- content.

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS class TEXT
        CHECK (class IS NULL OR class IN ('rule', 'preference', 'memory'));

CREATE INDEX IF NOT EXISTS idx_memories_class
    ON memories (class)
    WHERE class IS NOT NULL;
