-- Rollback for 009_audit_events.sql
--
-- Destructive: drops the audit trail. Save the rows out of band
-- first if you want to preserve them.

BEGIN;

DROP INDEX IF EXISTS idx_audit_events_actor_at;
DROP INDEX IF EXISTS idx_audit_events_event_at;
DROP INDEX IF EXISTS idx_audit_events_at;

DROP TABLE IF EXISTS audit_events;

DELETE FROM schema_migrations WHERE filename = '009_audit_events.sql';

COMMIT;
