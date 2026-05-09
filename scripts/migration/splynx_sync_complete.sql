-- ============================================================
-- Splynx Migration Gap Completion Script
-- ============================================================
--
-- Idempotent: safe to run multiple times.
-- Uses NOT EXISTS / ON CONFLICT to prevent duplicates.
--
-- Gaps addressed:
--   1. device_metrics:            ~125K monitoring_log entries
--   2. subscriber_custom_fields:  ~20K  customers_values entries
--   3. external_references:       ~11K  accounting_customers entries
--   4. kpi_aggregates:            MRR verification & backfill
--
-- Prerequisites:
--   - splynx_staging schema with all staging tables populated
--   - map_customers, map_monitoring mapping tables populated
--   - Phase 1 + Phase 2 migrations already executed
-- ============================================================


-- ============================================================
-- STEP 1: Fix device mapping for deleted Splynx monitoring
--         records and import their ~125K log entries
-- ============================================================
-- The original migration only seeded map_monitoring for
-- non-deleted devices. All 229 unmapped monitoring records
-- are deleted=true in Splynx (decommissioned infrastructure).
--
-- Sub-steps:
--   1a. 180 truly orphaned: create inactive network_devices
--   1b. 49 IP-matchable: map to existing network_devices
--   1c. Seed map_monitoring for all newly mapped devices
--   1d. Insert device_metrics for all newly mapped logs
-- ============================================================

-- 1a. Seed map_monitoring for IP-matchable devices (49):
--     Deleted Splynx entries whose IP matches an existing
--     network_device (renamed/re-added devices in Splynx).
INSERT INTO splynx_staging.map_monitoring (splynx_monitoring_id, network_device_id)
SELECT sm.id, nd.id
FROM splynx_staging.splynx_monitoring sm
JOIN network_devices nd ON nd.mgmt_ip = sm.ip
WHERE NOT EXISTS (
    SELECT 1 FROM splynx_staging.map_monitoring map
    WHERE map.splynx_monitoring_id = sm.id
)
AND NOT EXISTS (
    SELECT 1 FROM network_devices nd2
    WHERE nd2.splynx_monitoring_id = sm.id
)
ON CONFLICT DO NOTHING;


-- 1b. Seed map_monitoring for hostname-matchable devices (6):
--     Deleted Splynx entries whose title matches an existing
--     network_device hostname (same device, different IP in Splynx).
INSERT INTO splynx_staging.map_monitoring (splynx_monitoring_id, network_device_id)
SELECT sm.id, nd.id
FROM splynx_staging.splynx_monitoring sm
JOIN network_devices nd
    ON nd.hostname = substring(COALESCE(NULLIF(sm.title, ''), sm.ip) from 1 for 160)
WHERE NOT EXISTS (
    SELECT 1 FROM splynx_staging.map_monitoring map
    WHERE map.splynx_monitoring_id = sm.id
)
AND NOT EXISTS (
    SELECT 1 FROM network_devices nd2
    WHERE nd2.splynx_monitoring_id = sm.id
)
ON CONFLICT DO NOTHING;


-- 1c. Create network_devices for ~174 truly orphaned monitoring
--     records (deleted devices with no IP/hostname match).
--     Hostname is disambiguated with Splynx ID suffix to avoid
--     unique constraint violations from duplicate Splynx titles.
INSERT INTO network_devices (
    id,
    name,
    hostname,
    mgmt_ip,
    device_type,
    role,
    status,
    ping_enabled,
    snmp_enabled,
    notes,
    splynx_monitoring_id,
    is_active,
    current_subscriber_count,
    health_status,
    created_at,
    updated_at
)
SELECT
    gen_random_uuid(),
    substring(COALESCE(NULLIF(m.title, ''), m.ip) from 1 for 160),
    substring(
        COALESCE(NULLIF(m.title, ''), m.ip) || ' [splynx-' || m.id || ']'
        from 1 for 160
    ),
    m.ip,
    CASE
        WHEN m.type = 1 THEN 'router'
        WHEN m.type = 2 THEN 'switch'
        WHEN m.type = 3 THEN 'access_point'
        WHEN m.type = 4 THEN 'server'
        WHEN m.type = 5 THEN 'firewall'
        ELSE 'other'
    END::devicetype,
    'edge'::devicerole,
    'offline'::monitoring_devicestatus,
    true,
    false,
    'Imported from Splynx monitoring #' || m.id::text || ' (decommissioned)',
    m.id,
    false,   -- is_active=false: decommissioned device
    0,
    'unknown',
    NOW(),
    NOW()
FROM splynx_staging.splynx_monitoring m
WHERE NOT EXISTS (
    SELECT 1 FROM splynx_staging.map_monitoring map
    WHERE map.splynx_monitoring_id = m.id
)
AND NOT EXISTS (
    SELECT 1 FROM network_devices nd
    WHERE nd.splynx_monitoring_id = m.id
)
AND m.ip IS NOT NULL AND btrim(m.ip) <> '';


-- 1d. Seed map_monitoring for newly created orphaned devices:
--     Links via splynx_monitoring_id set in step 1c.
INSERT INTO splynx_staging.map_monitoring (splynx_monitoring_id, network_device_id)
SELECT sm.id, nd.id
FROM splynx_staging.splynx_monitoring sm
JOIN network_devices nd ON nd.splynx_monitoring_id = sm.id
WHERE NOT EXISTS (
    SELECT 1 FROM splynx_staging.map_monitoring map
    WHERE map.splynx_monitoring_id = sm.id
)
ON CONFLICT DO NOTHING;


-- 1d. Insert device_metrics for all newly mapped monitoring logs.
--     NOT EXISTS prevents duplicates on re-run.
INSERT INTO device_metrics (
    id,
    device_id,
    interface_id,
    metric_type,
    value,
    unit,
    recorded_at,
    created_at
)
SELECT
    gen_random_uuid(),
    map.network_device_id,
    NULL,
    'custom'::metrictype,
    CASE WHEN ml.status = 'ok' THEN 1.0 ELSE 0.0 END,
    ml.type || '_status',
    ml.date::timestamp with time zone,
    ml.date::timestamp with time zone
FROM splynx_staging.splynx_monitoring_log ml
JOIN splynx_staging.map_monitoring map
    ON map.splynx_monitoring_id = ml.monitoring_id
WHERE NOT EXISTS (
    SELECT 1
    FROM device_metrics dm
    WHERE dm.device_id = map.network_device_id
      AND dm.recorded_at = ml.date::timestamp with time zone
      AND dm.unit = ml.type || '_status'
);


-- ============================================================
-- STEP 2: Complete subscriber_custom_fields from
--         splynx_customers_values
-- ============================================================
-- Original: Section 15 of splynx_migrate.sql
-- Gaps:
--   social_id:        11,144 in staging, 0 migrated
--   zoho_id:           9,937 in staging, 2,799 migrated
--   splynx_reseller:     909 in staging, 61 migrated
--
-- ON CONFLICT on the unique constraint (subscriber_id, key)
-- prevents duplicates on re-runs.
-- ============================================================

-- 2a: zoho_id
INSERT INTO subscriber_custom_fields (
    id, subscriber_id, key, value_type, value_text,
    is_active, created_at, updated_at
)
SELECT
    gen_random_uuid(),
    map.subscriber_id,
    'zoho_id',
    'string',
    cv.value,
    true,
    NOW(),
    NOW()
FROM splynx_staging.splynx_customers_values cv
JOIN splynx_staging.map_customers map
    ON map.splynx_customer_id = cv.id
WHERE cv.name = 'zoho_id'
  AND cv.value IS NOT NULL
  AND btrim(cv.value) <> ''
ON CONFLICT ON CONSTRAINT uq_subscriber_custom_fields_subscriber_key
DO NOTHING;


-- 2b: social_id
INSERT INTO subscriber_custom_fields (
    id, subscriber_id, key, value_type, value_text,
    is_active, created_at, updated_at
)
SELECT
    gen_random_uuid(),
    map.subscriber_id,
    'social_id',
    'string',
    cv.value,
    true,
    NOW(),
    NOW()
FROM splynx_staging.splynx_customers_values cv
JOIN splynx_staging.map_customers map
    ON map.splynx_customer_id = cv.id
WHERE cv.name = 'social_id'
  AND cv.value IS NOT NULL
  AND btrim(cv.value) <> ''
ON CONFLICT ON CONSTRAINT uq_subscriber_custom_fields_subscriber_key
DO NOTHING;


-- 2c: splynx_reseller (from splynx_addon_resellers_reseller)
INSERT INTO subscriber_custom_fields (
    id, subscriber_id, key, value_type, value_text,
    is_active, created_at, updated_at
)
SELECT
    gen_random_uuid(),
    map.subscriber_id,
    'splynx_reseller',
    'string',
    cv.value,
    true,
    NOW(),
    NOW()
FROM splynx_staging.splynx_customers_values cv
JOIN splynx_staging.map_customers map
    ON map.splynx_customer_id = cv.id
WHERE cv.name = 'splynx_addon_resellers_reseller'
  AND cv.value IS NOT NULL
  AND btrim(cv.value) <> ''
ON CONFLICT ON CONSTRAINT uq_subscriber_custom_fields_subscriber_key
DO NOTHING;


-- ============================================================
-- STEP 3: Complete external_references from
--         splynx_accounting_customers
-- ============================================================
-- Original: Section 16 of splynx_migrate.sql
-- Gap: ~11K accounting records for mapped customers not yet
--      imported. connector_config_id is NULL for Splynx records,
--      so unique constraints with NULLs don't prevent duplicates;
--      we use NOT EXISTS instead.
-- ============================================================

INSERT INTO external_references (
    id,
    entity_type,
    entity_id,
    external_id,
    metadata,
    is_active,
    created_at,
    updated_at
)
SELECT
    gen_random_uuid(),
    'subscriber'::externalentitytype,
    map.subscriber_id,
    ac.accounting_id,
    json_build_object(
        'source', 'splynx_accounting',
        'splynx_customer_id', ac.customer_id,
        'accounting_status', ac.accounting_status
    ),
    NOT COALESCE(ac.deleted, false),
    NOW(),
    NOW()
FROM splynx_staging.splynx_accounting_customers ac
JOIN splynx_staging.map_customers map
    ON map.splynx_customer_id = ac.customer_id
WHERE ac.accounting_id IS NOT NULL
  AND btrim(ac.accounting_id) <> ''
  AND NOT EXISTS (
      SELECT 1
      FROM external_references er
      WHERE er.entity_type = 'subscriber'
        AND er.entity_id = map.subscriber_id
        AND er.external_id = ac.accounting_id
        AND er.connector_config_id IS NULL
  );


-- ============================================================
-- STEP 4: Verify & backfill kpi_aggregates from
--         splynx_mrr_statistics
-- ============================================================
-- Original: Section 19 of splynx_migrate.sql
-- The 3.3M -> 74K reduction is by design (GROUP BY date,
-- partner_id, location_id). Re-run the aggregation insert
-- to catch any (date, partner, location) combos that were
-- missed or added to staging after the initial migration.
-- ============================================================

INSERT INTO kpi_aggregates (
    id,
    key,
    period_start,
    period_end,
    value,
    metadata,
    created_at
)
SELECT
    gen_random_uuid(),
    'mrr_' || mrr.partner_id || '_' || mrr.location_id,
    mrr.date::timestamp with time zone,
    (mrr.date + interval '1 month')::timestamp with time zone,
    mrr.total_mrr,
    json_build_object(
        'source', 'splynx_mrr',
        'partner_id', mrr.partner_id,
        'location_id', mrr.location_id,
        'customer_count', mrr.customer_count
    ),
    NOW()
FROM (
    SELECT
        date,
        partner_id,
        location_id,
        sum(total) AS total_mrr,
        count(DISTINCT customer_id) AS customer_count
    FROM splynx_staging.splynx_mrr_statistics
    GROUP BY date, partner_id, location_id
) mrr
WHERE NOT EXISTS (
    SELECT 1
    FROM kpi_aggregates ka
    WHERE ka.key = 'mrr_' || mrr.partner_id || '_' || mrr.location_id
      AND ka.period_start = mrr.date::timestamp with time zone
);


-- ============================================================
-- VERIFICATION QUERIES
-- ============================================================
-- Run these after the inserts to confirm gaps are closed.
-- Uncomment to execute as part of the script, or run manually.
-- ============================================================

-- -- Device metrics: mapped log count vs device_metrics count
-- SELECT
--     'device_metrics' AS target,
--     (SELECT count(*) FROM splynx_staging.splynx_monitoring_log ml
--      JOIN splynx_staging.map_monitoring map
--        ON map.splynx_monitoring_id = ml.monitoring_id) AS staging_mapped,
--     (SELECT count(*) FROM device_metrics
--      WHERE metric_type = 'custom'
--        AND unit LIKE '%_status') AS migrated;
--
-- -- Subscriber custom fields: per-key counts
-- SELECT
--     cv.name AS field,
--     count(*) AS staging_count,
--     (SELECT count(*) FROM subscriber_custom_fields scf
--      WHERE scf.key = CASE
--          WHEN cv.name = 'splynx_addon_resellers_reseller' THEN 'splynx_reseller'
--          ELSE cv.name END) AS migrated_count
-- FROM splynx_staging.splynx_customers_values cv
-- JOIN splynx_staging.map_customers map ON map.splynx_customer_id = cv.id
-- WHERE cv.name IN ('zoho_id', 'social_id', 'splynx_addon_resellers_reseller')
--   AND cv.value IS NOT NULL AND btrim(cv.value) <> ''
-- GROUP BY cv.name;
--
-- -- External references: accounting records
-- SELECT
--     'external_references' AS target,
--     (SELECT count(*) FROM splynx_staging.splynx_accounting_customers ac
--      JOIN splynx_staging.map_customers map
--        ON map.splynx_customer_id = ac.customer_id
--      WHERE ac.accounting_id IS NOT NULL
--        AND btrim(ac.accounting_id) <> '') AS staging_mapped,
--     (SELECT count(*) FROM external_references
--      WHERE connector_config_id IS NULL
--        AND entity_type = 'subscriber') AS migrated;
--
-- -- KPI aggregates: MRR date range coverage
-- SELECT
--     'kpi_aggregates' AS target,
--     (SELECT count(DISTINCT (date, partner_id, location_id))
--      FROM splynx_staging.splynx_mrr_statistics) AS staging_combos,
--     (SELECT count(*) FROM kpi_aggregates
--      WHERE key LIKE 'mrr_%') AS migrated,
--     (SELECT min(period_start) FROM kpi_aggregates
--      WHERE key LIKE 'mrr_%') AS min_date,
--     (SELECT max(period_start) FROM kpi_aggregates
--      WHERE key LIKE 'mrr_%') AS max_date;
