-- Migration 009: audit_events table
--
-- Captures security-relevant actions for forensics and user-visible
-- account activity. Events are append-only; the table is never
-- updated or deleted from by application code. Retention policy is
-- operational (vacuum / archive via separate job if volume warrants).
--
-- Schema:
--   actor_user_id  -- Who did it. NULL for pre-auth events (failed
--                     login on an unknown username, rate-limited
--                     attempts with no identity).
--   event_type     -- Short dotted string: login.success, login.fail,
--                     logout, token.mint, token.revoke, user.register,
--                     rate_limit.hit. Easy to filter / index.
--   target_id      -- Optional UUID the event acted on. e.g. a token
--                     id for token.mint / token.revoke, a user id for
--                     a future admin off-board event.
--   detail         -- jsonb with event-specific context. IP, user
--                     agent, reason codes, etc. No passwords, no
--                     raw tokens.
--   at             -- TIMESTAMPTZ, default now(). Indexed for time-
--                     range queries.
--
-- Rollback: config/migrations/rollback/009_audit_events.sql

CREATE TABLE IF NOT EXISTS audit_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    actor_user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    target_id UUID,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "Show me this user's recent account activity" — the common query
-- for a future user-facing security page.
CREATE INDEX IF NOT EXISTS idx_audit_events_actor_at
    ON audit_events (actor_user_id, at DESC);

-- Event-type scans for incident response / rate-cohort analysis.
CREATE INDEX IF NOT EXISTS idx_audit_events_event_at
    ON audit_events (event_type, at DESC);

-- Time-range scans for "what happened on date X" style forensics.
CREATE INDEX IF NOT EXISTS idx_audit_events_at
    ON audit_events (at DESC);
