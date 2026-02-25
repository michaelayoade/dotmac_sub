-- Database audit follow-up: nas_devices performance indexes + unallocated payment report.
-- Target DB: dotmac_sub
--
-- Notes:
-- 1) Run index statements during a low-traffic window.
-- 2) CREATE INDEX CONCURRENTLY cannot run inside a transaction block.

-- Optional, for ILIKE search acceleration on text fields.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- =============================================================================
-- NAS DEVICES: RECOMMENDED INDEXES
-- =============================================================================

-- Supports common list/filter path:
--   WHERE is_active [AND vendor] [AND status] [AND pop_site_id]
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_nas_devices_active_vendor_status
    ON public.nas_devices (vendor, status)
    WHERE is_active IS TRUE;

-- Supports pop-site scoped listing/counting for active devices.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_nas_devices_active_pop_site
    ON public.nas_devices (pop_site_id)
    WHERE is_active IS TRUE;

-- Supports radius sync path:
--   WHERE is_active IS TRUE AND ip_address IS NOT NULL
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_nas_devices_active_ip
    ON public.nas_devices (ip_address)
    WHERE is_active IS TRUE AND ip_address IS NOT NULL;

-- Supports ordered active list:
--   WHERE is_active IS TRUE ORDER BY name
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_nas_devices_active_name
    ON public.nas_devices (name)
    WHERE is_active IS TRUE;

-- Supports typeahead ILIKE searches on active devices.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_nas_devices_name_trgm
    ON public.nas_devices USING gin (name gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_nas_devices_code_trgm
    ON public.nas_devices USING gin (code gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_nas_devices_ip_trgm
    ON public.nas_devices USING gin (ip_address gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_nas_devices_management_ip_trgm
    ON public.nas_devices USING gin (management_ip gin_trgm_ops);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_nas_devices_nas_ip_trgm
    ON public.nas_devices USING gin (nas_ip gin_trgm_ops);

-- =============================================================================
-- BILLING AUDIT: UNALLOCATED PAYMENTS BY STATUS + AGE
-- =============================================================================

WITH unallocated AS (
    SELECT
        p.id,
        p.status::text AS status,
        COALESCE(p.paid_at, p.created_at) AS effective_at,
        p.amount
    FROM public.payments p
    LEFT JOIN public.payment_allocations pa
        ON pa.payment_id = p.id
    WHERE pa.id IS NULL
),
bucketed AS (
    SELECT
        status,
        CASE
            WHEN effective_at >= NOW() - INTERVAL '7 days' THEN '0-7 days'
            WHEN effective_at >= NOW() - INTERVAL '30 days' THEN '8-30 days'
            WHEN effective_at >= NOW() - INTERVAL '90 days' THEN '31-90 days'
            ELSE '90+ days'
        END AS age_bucket,
        COUNT(*) AS payment_count,
        COALESCE(SUM(amount), 0) AS total_amount
    FROM unallocated
    GROUP BY status, age_bucket
)
SELECT
    status,
    age_bucket,
    payment_count,
    total_amount
FROM bucketed
ORDER BY
    status,
    CASE age_bucket
        WHEN '0-7 days' THEN 1
        WHEN '8-30 days' THEN 2
        WHEN '31-90 days' THEN 3
        ELSE 4
    END;

