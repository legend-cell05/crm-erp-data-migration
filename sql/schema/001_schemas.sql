-- ===========================================================================
-- Schemas.
--
--   legacy     the Arcadia CRM as it exists today -- deliberately awful, and
--              not something keystone ever writes to.
--   migration  keystone's own memory: runs, crosswalk, rejects, profiles.
--
-- The separation is the point. Everything in `legacy` is a faithful copy of
-- somebody else's problem; everything in `migration` is ours, and is what
-- makes a second run an update rather than a duplicate.
-- ===========================================================================

CREATE SCHEMA IF NOT EXISTS ${LEGACY};
CREATE SCHEMA IF NOT EXISTS ${MIGRATION};

COMMENT ON SCHEMA ${LEGACY} IS 'Simulated legacy Arcadia CRM. Read-only for keystone. Synthetic data.';
COMMENT ON SCHEMA ${MIGRATION} IS 'keystone migration state: runs, crosswalk, rejects, profiles, impact.';
