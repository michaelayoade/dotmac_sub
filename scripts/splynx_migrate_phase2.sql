-- Phase 2: Batched migration of million-row tables from Splynx.
--
-- This script handles data too large for a single transaction:
--   A) Access credentials (from service logins) — prerequisite for RADIUS
--   B) Stage splynx_statistics from FDW in quarterly batches
--   C) RADIUS sessions → radius_accounting_sessions (monthly batches)
--   D) Stage splynx_traffic_counter from FDW in quarterly batches
--   E) Traffic counters → usage_records (monthly batches)
--   F) Post-migration performance indexes
--
-- Prerequisites:
--   1. alembic upgrade head (schema changes applied)
--   2. scripts/splynx_staging.sql (FDW + staging tables created)
--   3. scripts/splynx_migrate.sql (core data imported, mapping tables populated)
--
-- Run with: psql -f scripts/splynx_migrate_phase2.sql
-- Each section commits independently. Safe to re-run (ON CONFLICT DO NOTHING).

-- ============================================================================
-- SECTION A: Access Credentials from service logins
-- ============================================================================
-- Creates AccessCredential rows from Splynx service login/password data.
-- Required before RADIUS session import (sessions reference credentials).

CREATE TABLE IF NOT EXISTS splynx_staging.map_access_credentials (
    splynx_service_id integer PRIMARY KEY,
    access_credential_id uuid NOT NULL
);

INSERT INTO splynx_staging.map_access_credentials (splynx_service_id, access_credential_id)
SELECT id, gen_random_uuid()
FROM splynx_staging.splynx_services_internet
WHERE login IS NOT NULL AND btrim(login) <> ''
ON CONFLICT DO NOTHING;

INSERT INTO access_credentials (
    id,
    subscriber_id,
    username,
    is_active,
    created_at,
    updated_at
)
SELECT
    acmap.access_credential_id,
    cmap.subscriber_id,
    s.login,
    s.status = 'active',
    NOW(),
    NOW()
FROM splynx_staging.splynx_services_internet s
JOIN splynx_staging.map_access_credentials acmap ON acmap.splynx_service_id = s.id
JOIN splynx_staging.map_customers cmap ON cmap.splynx_customer_id = s.customer_id
WHERE s.login IS NOT NULL AND btrim(s.login) <> ''
ON CONFLICT ON CONSTRAINT uq_access_credentials_username DO NOTHING;

-- ============================================================================
-- SECTION B: Batch-stage splynx_statistics from FDW
-- ============================================================================
-- Pulls RADIUS session data from the remote Splynx DB in 3-month batches
-- to avoid overwhelming the FDW connection.

DO $$
DECLARE
    batch_start date;
    batch_end   date;
    min_date    date;
    max_date    date;
    row_count   bigint;
BEGIN
    -- Determine date range from the foreign table
    SELECT min(start_date::date), max(start_date::date)
    INTO min_date, max_date
    FROM splynx_fdw.splynx_statistics;

    IF min_date IS NULL THEN
        RAISE NOTICE 'No data in splynx_statistics — skipping staging';
        RETURN;
    END IF;

    batch_start := min_date;
    WHILE batch_start <= max_date LOOP
        batch_end := batch_start + interval '3 months';

        INSERT INTO splynx_staging.splynx_statistics
        SELECT *
        FROM splynx_fdw.splynx_statistics
        WHERE start_date >= batch_start::timestamp
          AND start_date < batch_end::timestamp
        ON CONFLICT DO NOTHING;

        GET DIAGNOSTICS row_count = ROW_COUNT;
        RAISE NOTICE 'Staged splynx_statistics % to %: % rows',
            batch_start, batch_end, row_count;

        batch_start := batch_end;
    END LOOP;
END
$$;

-- Index for efficient joins during migration
CREATE INDEX IF NOT EXISTS idx_staging_statistics_service_id
    ON splynx_staging.splynx_statistics (service_id);

-- ============================================================================
-- SECTION C: RADIUS Sessions → radius_accounting_sessions (batched monthly)
-- ============================================================================
-- Maps Splynx statistics rows to DotMac RADIUS accounting sessions.
-- Sessions without a mappable service get NULL subscription_id.

DO $$
DECLARE
    batch_start date;
    batch_end   date;
    min_date    date;
    max_date    date;
    row_count   bigint;
BEGIN
    SELECT min(start_date::date), max(start_date::date)
    INTO min_date, max_date
    FROM splynx_staging.splynx_statistics;

    IF min_date IS NULL THEN
        RAISE NOTICE 'No staged statistics — skipping RADIUS import';
        RETURN;
    END IF;

    batch_start := min_date;
    WHILE batch_start <= max_date LOOP
        batch_end := batch_start + interval '1 month';

        INSERT INTO radius_accounting_sessions (
            id,
            subscription_id,
            access_credential_id,
            radius_client_id,
            nas_device_id,
            session_id,
            status_type,
            session_start,
            session_end,
            input_octets,
            output_octets,
            terminate_cause,
            splynx_session_id,
            created_at
        )
        SELECT
            gen_random_uuid(),
            smap.subscription_id,
            ac.id,  -- NULL if credential wasn't inserted (username conflict)
            NULL,
            rmap.nas_device_id,
            COALESCE(st.session_id, 'splynx_' || st.id::text),
            'stop'::accountingstatus,
            (st.start_date::text || ' ' || COALESCE(st.start_time::text, '00:00:00'))::timestamp with time zone,
            (st.end_date::text || ' ' || COALESCE(st.end_time::text, '00:00:00'))::timestamp with time zone,
            st.in_bytes,
            st.out_bytes,
            st.terminate_cause::text,
            st.id,
            (st.start_date::text || ' ' || COALESCE(st.start_time::text, '00:00:00'))::timestamp with time zone
        FROM splynx_staging.splynx_statistics st
        LEFT JOIN splynx_staging.map_services smap
            ON smap.splynx_service_id = st.service_id
        LEFT JOIN splynx_staging.map_access_credentials acmap
            ON acmap.splynx_service_id = st.service_id
        LEFT JOIN access_credentials ac
            ON ac.id = acmap.access_credential_id
        LEFT JOIN splynx_staging.map_routers rmap
            ON rmap.splynx_router_id = st.nas_id
        WHERE st.start_date >= batch_start
          AND st.start_date < batch_end
        ON CONFLICT DO NOTHING;

        GET DIAGNOSTICS row_count = ROW_COUNT;
        RAISE NOTICE 'RADIUS sessions % to %: % rows', batch_start, batch_end, row_count;

        -- Commit each batch independently
        COMMIT;
        batch_start := batch_end;
    END LOOP;
END
$$;

-- ============================================================================
-- SECTION D: Batch-stage splynx_traffic_counter from FDW
-- ============================================================================

DO $$
DECLARE
    batch_start date;
    batch_end   date;
    min_date    date;
    max_date    date;
    row_count   bigint;
BEGIN
    SELECT min(date::date), max(date::date)
    INTO min_date, max_date
    FROM splynx_fdw.splynx_traffic_counter;

    IF min_date IS NULL THEN
        RAISE NOTICE 'No data in splynx_traffic_counter — skipping staging';
        RETURN;
    END IF;

    batch_start := min_date;
    WHILE batch_start <= max_date LOOP
        batch_end := batch_start + interval '3 months';

        INSERT INTO splynx_staging.splynx_traffic_counter
        SELECT *
        FROM splynx_fdw.splynx_traffic_counter
        WHERE date >= batch_start
          AND date < batch_end
        ON CONFLICT DO NOTHING;

        GET DIAGNOSTICS row_count = ROW_COUNT;
        RAISE NOTICE 'Staged splynx_traffic_counter % to %: % rows',
            batch_start, batch_end, row_count;

        batch_start := batch_end;
    END LOOP;
END
$$;

-- Index for efficient joins during migration
CREATE INDEX IF NOT EXISTS idx_staging_traffic_service_id
    ON splynx_staging.splynx_traffic_counter (service_id);

-- ============================================================================
-- SECTION E: Traffic Counters → usage_records (batched monthly)
-- ============================================================================
-- Converts Splynx traffic counter rows to DotMac usage_records.
-- Only imports rows with a mappable service (subscription_id NOT NULL).
-- Bytes are converted to GB (÷ 1073741824).

DO $$
DECLARE
    batch_start date;
    batch_end   date;
    min_date    date;
    max_date    date;
    row_count   bigint;
BEGIN
    SELECT min(date), max(date)
    INTO min_date, max_date
    FROM splynx_staging.splynx_traffic_counter;

    IF min_date IS NULL THEN
        RAISE NOTICE 'No staged traffic counters — skipping usage import';
        RETURN;
    END IF;

    batch_start := min_date;
    WHILE batch_start <= max_date LOOP
        batch_end := batch_start + interval '1 month';

        INSERT INTO usage_records (
            id,
            subscription_id,
            quota_bucket_id,
            source,
            recorded_at,
            input_gb,
            output_gb,
            total_gb,
            created_at
        )
        SELECT
            gen_random_uuid(),
            smap.subscription_id,
            NULL,
            'radius'::usagesource,
            tc.date::timestamp with time zone,
            ROUND(tc.up_bytes::numeric / 1073741824, 4),
            ROUND(tc.down_bytes::numeric / 1073741824, 4),
            ROUND((tc.up_bytes::numeric + tc.down_bytes::numeric) / 1073741824, 4),
            tc.date::timestamp with time zone
        FROM splynx_staging.splynx_traffic_counter tc
        INNER JOIN splynx_staging.map_services smap
            ON smap.splynx_service_id = tc.service_id
        WHERE tc.date >= batch_start
          AND tc.date < batch_end
        ON CONFLICT DO NOTHING;

        GET DIAGNOSTICS row_count = ROW_COUNT;
        RAISE NOTICE 'Usage records % to %: % rows', batch_start, batch_end, row_count;

        -- Commit each batch independently
        COMMIT;
        batch_start := batch_end;
    END LOOP;
END
$$;

-- ============================================================================
-- SECTION F: Post-migration performance indexes
-- ============================================================================
-- Created CONCURRENTLY so they don't lock the tables during creation.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_device_metrics_device_recorded
    ON device_metrics (device_id, recorded_at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_radius_sessions_subscription_start
    ON radius_accounting_sessions (subscription_id, session_start);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_usage_records_subscription_recorded
    ON usage_records (subscription_id, recorded_at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_radius_sessions_splynx_session_id
    ON radius_accounting_sessions (splynx_session_id)
    WHERE splynx_session_id IS NOT NULL;
