-- Backfill invoice fields for records created by incremental sync.
--
-- The incremental sync (scripts/migration/incremental_sync.py) had bugs:
--   1. due_at used date_payment instead of date_till
--   2. subtotal / tax_total were not set (defaulted to 0)
--   3. billing_period_start / billing_period_end were not set
--   4. invoice_lines.subscription_id was not set
--
-- This script also:
--   5. Backfills subscriptions.next_billing_at from latest invoice periods
--   6. Soft-deletes ticket/equipment-request records mistakenly imported as subscribers
--
-- Safe to run multiple times (all updates are idempotent).
-- Run against the DotMac Sub PostgreSQL database.

BEGIN;

-- ============================================================================
-- 1. Fix due_at on incrementally-synced invoices
--    Identify them by: splynx_invoice_id IS NOT NULL AND billing_period_start IS NULL
--    (bulk migration populated billing_period_start; incremental sync did not)
-- ============================================================================

UPDATE invoices inv
SET due_at = si.date_till::timestamp with time zone
FROM splynx_staging.splynx_invoices si
WHERE inv.splynx_invoice_id = si.id
  AND inv.billing_period_start IS NULL
  AND si.date_till IS NOT NULL;

-- Log count
DO $$
DECLARE
    cnt integer;
BEGIN
    GET DIAGNOSTICS cnt = ROW_COUNT;
    RAISE NOTICE 'Step 1: Updated due_at on % invoices', cnt;
END $$;


-- ============================================================================
-- 2. Backfill subtotal, tax_total, billing_period_start, billing_period_end
--    from Splynx invoice_items via splynx_staging
-- ============================================================================

WITH item_agg AS (
    SELECT
        li.invoice_id AS splynx_invoice_id,
        SUM(COALESCE(li.price, 0) * COALESCE(li.quantity, 1)) AS subtotal,
        SUM(COALESCE(li.price, 0) * COALESCE(li.quantity, 1) * COALESCE(li.tax, 0) / 100.0) AS tax_total,
        MIN(li.period_from)::timestamp with time zone AS billing_period_start,
        MAX(li.period_to)::timestamp with time zone AS billing_period_end
    FROM splynx_staging.splynx_invoice_items li
    WHERE li.deleted = '0'
    GROUP BY li.invoice_id
)
UPDATE invoices inv
SET
    subtotal   = COALESCE(ia.subtotal, inv.total),
    tax_total  = COALESCE(ia.tax_total, 0),
    billing_period_start = COALESCE(ia.billing_period_start, inv.issued_at),
    billing_period_end   = COALESCE(ia.billing_period_end, inv.due_at)
FROM item_agg ia
WHERE inv.splynx_invoice_id = ia.splynx_invoice_id
  AND inv.billing_period_start IS NULL;

DO $$
DECLARE
    cnt integer;
BEGIN
    GET DIAGNOSTICS cnt = ROW_COUNT;
    RAISE NOTICE 'Step 2: Backfilled subtotal/tax/period on % invoices', cnt;
END $$;


-- ============================================================================
-- 3. Backfill invoice_lines.subscription_id via billing_transactions → services
-- ============================================================================

WITH line_subs AS (
    SELECT
        il.id AS line_id,
        svc_map.dotmac_id AS subscription_id
    FROM invoice_lines il
    JOIN invoices inv ON inv.id = il.invoice_id
    -- Match line to Splynx invoice_item by invoice + description + amount
    JOIN splynx_staging.splynx_invoice_items sli
        ON sli.invoice_id = inv.splynx_invoice_id
        AND COALESCE(LEFT(sli.description, 255), '') = COALESCE(il.description, '')
        AND ABS(COALESCE(sli.price * sli.quantity, 0) - il.amount) < 0.01
    -- billing_transactions maps transaction_id → service_id
    JOIN splynx_staging.splynx_billing_transactions bt
        ON bt.id = sli.transaction_id
    -- splynx_id_mappings maps service_id → subscription UUID
    JOIN splynx_id_mappings svc_map
        ON svc_map.entity_type = 'service'
        AND svc_map.splynx_id = bt.service_id
    WHERE il.subscription_id IS NULL
      AND sli.transaction_id IS NOT NULL
      AND bt.service_id IS NOT NULL
)
UPDATE invoice_lines il
SET subscription_id = ls.subscription_id
FROM line_subs ls
WHERE il.id = ls.line_id;

DO $$
DECLARE
    cnt integer;
BEGIN
    GET DIAGNOSTICS cnt = ROW_COUNT;
    RAISE NOTICE 'Step 3: Linked % invoice lines to subscriptions', cnt;
END $$;


-- ============================================================================
-- 4. Backfill subscriptions.next_billing_at from latest invoice period_to
-- ============================================================================

WITH latest_period AS (
    SELECT
        il.subscription_id,
        MAX(inv.billing_period_end) AS last_period_end
    FROM invoice_lines il
    JOIN invoices inv ON inv.id = il.invoice_id
    WHERE il.subscription_id IS NOT NULL
      AND inv.billing_period_end IS NOT NULL
      AND inv.status != 'void'
    GROUP BY il.subscription_id
)
UPDATE subscriptions s
SET next_billing_at = lp.last_period_end + interval '1 day'
FROM latest_period lp
WHERE s.id = lp.subscription_id
  AND s.status = 'active'
  AND (s.next_billing_at IS NULL OR s.next_billing_at < lp.last_period_end);

DO $$
DECLARE
    cnt integer;
BEGIN
    GET DIAGNOSTICS cnt = ROW_COUNT;
    RAISE NOTICE 'Step 4: Set next_billing_at on % subscriptions', cnt;
END $$;


-- ============================================================================
-- 5. Soft-delete ticket/equipment-request subscribers
--    These are ~61 Splynx records where the "customer" was actually an
--    equipment request or fault ticket (e.g. "Request for Mikrotik CCR1072").
--    They have placeholder emails like no-email+NNNNN@splynx.local.
-- ============================================================================

UPDATE subscribers
SET
    is_active = false,
    updated_at = NOW()
WHERE email LIKE 'no-email+%@splynx.local'
  AND (
    first_name ~* '^(request|faulty|fault|replace|swap|repair|return|1 )'
    OR last_name ~* '(request$|onu$|swap$|replace$|cable$)'
  )
  AND NOT EXISTS (
    SELECT 1 FROM subscriptions
    WHERE subscriptions.subscriber_id = subscribers.id
      AND subscriptions.status = 'active'
  )
  AND is_active = true;

DO $$
DECLARE
    cnt integer;
BEGIN
    GET DIAGNOSTICS cnt = ROW_COUNT;
    RAISE NOTICE 'Step 5: Soft-deleted % ticket-type subscribers', cnt;
END $$;


COMMIT;
