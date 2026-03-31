-- Migration 004: Memory scope hierarchy
-- Adds three-tier scoping so memories can apply to:
--   - local   : single project (default, current behavior)
--   - domain  : a category like a language, tool, or platform
--   - global  : every project, everywhere
--
-- Search and context tools automatically include global memories and
-- any domain memories matching the current project's declared domains.
-- Projects declare which domains apply via the domains[] array.

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'local'
        CHECK (scope IN ('local', 'domain', 'global'));

ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS domains TEXT[] NOT NULL DEFAULT '{}';

-- Index for efficient scope-based lookups during search
CREATE INDEX IF NOT EXISTS idx_projects_scope
    ON projects (scope);

CREATE INDEX IF NOT EXISTS idx_projects_domains
    ON projects USING gin (domains);
