-- Idempotent upgrade for existing FreeRADIUS PostgreSQL deployments.
-- Apply with:  psql -U radius -d radius -f upgrade_001_security_perf.sql
--
-- Adds:
--   * BlastRADIUS mitigation columns on nas
--   * Forensic columns on radpostauth (NAS + station IDs)
--   * `class` column on radacct (for Auth/Acct correlation)
--   * Partial indexes for active-session queries
-- Migrates radacct.nasipaddress and radacct.framedipaddress to INET in place.
-- Safe to re-run.

BEGIN;

-- nas: BlastRADIUS mitigation (CVE-2024-3596)
ALTER TABLE nas
    ADD COLUMN IF NOT EXISTS require_message_authenticator BOOLEAN DEFAULT TRUE;
ALTER TABLE nas
    ADD COLUMN IF NOT EXISTS limit_proxy_state BOOLEAN DEFAULT FALSE;

-- radpostauth: forensic context for reject investigations
ALTER TABLE radpostauth
    ADD COLUMN IF NOT EXISTS nasipaddress INET;
ALTER TABLE radpostauth
    ADD COLUMN IF NOT EXISTS calledstationid VARCHAR(50);
ALTER TABLE radpostauth
    ADD COLUMN IF NOT EXISTS callingstationid VARCHAR(50);

-- radacct: Class attribute for correlating Access-Accept with accounting
ALTER TABLE radacct
    ADD COLUMN IF NOT EXISTS class VARCHAR(64);

-- radacct: widen session time to BIGINT (Postgres allows narrowing safely)
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'radacct'
          AND column_name = 'acctsessiontime'
          AND data_type = 'integer'
    ) THEN
        ALTER TABLE radacct ALTER COLUMN acctsessiontime TYPE BIGINT;
    END IF;
END$$;

-- radacct: convert nasipaddress / framedipaddress to INET if currently text.
-- USING handles existing string data; rows with invalid IPs will block the
-- migration so they can be inspected manually (intentional — silent NULLing
-- would erase forensic data).
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'radacct'
          AND column_name = 'nasipaddress'
          AND udt_name <> 'inet'
    ) THEN
        ALTER TABLE radacct
            ALTER COLUMN nasipaddress TYPE INET
            USING NULLIF(nasipaddress, '')::inet;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'radacct'
          AND column_name = 'framedipaddress'
          AND udt_name <> 'inet'
    ) THEN
        ALTER TABLE radacct
            ALTER COLUMN framedipaddress TYPE INET
            USING NULLIF(framedipaddress, '')::inet;
    END IF;
END$$;

-- Partial indexes for active-session hot paths.
CREATE INDEX IF NOT EXISTS idx_radacct_active_user
    ON radacct(username) WHERE acctstoptime IS NULL;
CREATE INDEX IF NOT EXISTS idx_radacct_active_nas
    ON radacct(nasipaddress) WHERE acctstoptime IS NULL;

COMMIT;
