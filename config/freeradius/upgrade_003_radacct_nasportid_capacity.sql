-- Widen NAS-Port-Id storage to the maximum RADIUS string-attribute payload.
--
-- Several NAS vendors send descriptive interface paths longer than the
-- historical FreeRADIUS VARCHAR(32) default. PostgreSQL rejects the entire
-- accounting INSERT when that happens, leaving the customer session and usage
-- invisible to enforcement and reconciliation.
--
-- Apply with:
--   psql -v ON_ERROR_STOP=1 -U radius -d radius \
--     -f upgrade_003_radacct_nasportid_capacity.sql
--
-- Safe to re-run. Widening VARCHAR is metadata-only on supported PostgreSQL
-- releases, but still takes a brief ACCESS EXCLUSIVE table lock.

BEGIN;
SET LOCAL lock_timeout = '5s';

DO $$
DECLARE
    target_table TEXT;
    current_length INTEGER;
BEGIN
    FOREACH target_table IN ARRAY ARRAY['radacct', 'radacct_admin']
    LOOP
        SELECT character_maximum_length
          INTO current_length
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = target_table
           AND column_name = 'nasportid';

        IF FOUND AND (current_length IS NULL OR current_length < 253) THEN
            -- NULL means an already-unbounded TEXT column; leave it unchanged.
            IF current_length IS NOT NULL THEN
                EXECUTE format(
                    'ALTER TABLE %I ALTER COLUMN nasportid TYPE VARCHAR(253)',
                    target_table
                );
            END IF;
        END IF;
    END LOOP;
END$$;

COMMIT;
