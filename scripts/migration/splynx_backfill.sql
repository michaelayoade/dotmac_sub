-- Backfill Splynx-specific fields into extended columns after migration.
-- Run AFTER splynx_migrate.sql completes.

BEGIN;

-- Catalog offers from splynx tariffs
UPDATE catalog_offers o
SET
    splynx_tariff_id = t.id,
    splynx_service_name = substring(t.service_name from 1 for 160),
    splynx_tax_id = t.tax_id,
    with_vat = COALESCE(t.with_vat, false),
    vat_percent = t.vat_percent,
    speed_download_mbps = t.speed_download,
    speed_upload_mbps = t.speed_upload,
    aggregation = t.aggregation,
    priority = t.priority,
    available_for_services = COALESCE(t.available_for_services, true),
    show_on_customer_portal = COALESCE(t.show_on_customer_portal, true)
FROM splynx_staging.map_tariffs map
JOIN splynx_staging.splynx_tariffs_internet t ON t.id = map.splynx_tariff_id
WHERE o.id = map.offer_id;

-- Subscriptions from splynx services
UPDATE subscriptions s
SET
    splynx_service_id = svc.id,
    router_id = svc.router_id,
    service_description = svc.description,
    quantity = svc.quantity,
    unit = svc.unit,
    unit_price = svc.unit_price,
    discount = COALESCE(svc.discount, false),
    discount_value = svc.discount_value,
    discount_type = svc.discount_type,
    service_status_raw = svc.status,
    login = svc.login,
    ipv4_address = svc.ipv4,
    ipv6_address = svc.ipv6,
    mac_address = svc.mac
FROM splynx_staging.map_services map
JOIN splynx_staging.splynx_services_internet svc ON svc.id = map.splynx_service_id
WHERE s.id = map.subscription_id;

-- Subscriber billing fields from splynx customer_billing
UPDATE subscribers sub
SET
    billing_enabled = COALESCE(b.enabled, true),
    billing_name = b.billing_person,
    billing_address_line1 = substring(b.billing_street_1 from 1 for 160),
    billing_city = b.billing_city,
    billing_postal_code = substring(b.billing_zip_code from 1 for 20),
    deposit = b.deposit,
    payment_method = b.payment_method,
    billing_day = b.billing_date,
    payment_due_days = b.billing_due,
    grace_period_days = b.grace_period,
    min_balance = b.min_balance
FROM splynx_staging.map_customers map
JOIN splynx_staging.splynx_customer_billing b ON b.customer_id = map.splynx_customer_id
WHERE sub.id = map.subscriber_id;

-- Subscriber created/updated dates from splynx customers
UPDATE subscribers sub
SET
    created_at = COALESCE(c.date_add::timestamp with time zone, sub.created_at),
    updated_at = COALESCE(c.last_update, sub.updated_at)
FROM splynx_staging.map_customers map
JOIN splynx_staging.splynx_customers c ON c.id = map.splynx_customer_id
WHERE sub.id = map.subscriber_id;

-- ============================================================================
-- Backfill splynx_customer_id on subscribers from map_customers
-- ============================================================================

UPDATE subscribers sub
SET splynx_customer_id = map.splynx_customer_id
FROM splynx_staging.map_customers map
WHERE sub.id = map.subscriber_id
    AND sub.splynx_customer_id IS NULL;

-- ============================================================================
-- Backfill splynx_invoice_id on invoices from map_invoices
-- ============================================================================

UPDATE invoices inv
SET splynx_invoice_id = map.splynx_invoice_id
FROM splynx_staging.map_invoices map
WHERE inv.id = map.invoice_id
    AND inv.splynx_invoice_id IS NULL;

-- ============================================================================
-- Backfill splynx_payment_id on payments from map_payments
-- ============================================================================

UPDATE payments pay
SET splynx_payment_id = map.splynx_payment_id
FROM splynx_staging.map_payments map
WHERE pay.id = map.payment_id
    AND pay.splynx_payment_id IS NULL;

-- ============================================================================
-- Preserve Splynx login as subscriber_custom_fields entry
-- ============================================================================
-- Stores the original Splynx login (used as subscriber_number before) for reference.

INSERT INTO subscriber_custom_fields (
    id, subscriber_id, key, value_type, value_text, is_active, created_at, updated_at
)
SELECT
    gen_random_uuid(),
    map.subscriber_id,
    'splynx_login',
    'string',
    c.login,
    true,
    NOW(),
    NOW()
FROM splynx_staging.splynx_customers c
JOIN splynx_staging.map_customers map ON map.splynx_customer_id = c.id
WHERE c.login IS NOT NULL AND btrim(c.login) <> ''
ON CONFLICT ON CONSTRAINT uq_subscriber_custom_fields_subscriber_key DO NOTHING;

-- ============================================================================
-- Customer info fields → subscriber custom fields
-- ============================================================================

INSERT INTO subscriber_custom_fields (
    id, subscriber_id, key, value_type, value_text, is_active, created_at, updated_at
)
SELECT
    gen_random_uuid(),
    map.subscriber_id,
    'company_id',
    'string',
    ci.company_id,
    true,
    NOW(),
    NOW()
FROM splynx_staging.splynx_customer_info ci
JOIN splynx_staging.map_customers map ON map.splynx_customer_id = ci.customer_id
WHERE ci.company_id IS NOT NULL AND btrim(ci.company_id) <> ''
ON CONFLICT ON CONSTRAINT uq_subscriber_custom_fields_subscriber_key DO NOTHING;

INSERT INTO subscriber_custom_fields (
    id, subscriber_id, key, value_type, value_text, is_active, created_at, updated_at
)
SELECT
    gen_random_uuid(),
    map.subscriber_id,
    'vat_id',
    'string',
    ci.vat_id,
    true,
    NOW(),
    NOW()
FROM splynx_staging.splynx_customer_info ci
JOIN splynx_staging.map_customers map ON map.splynx_customer_id = ci.customer_id
WHERE ci.vat_id IS NOT NULL AND btrim(ci.vat_id) <> ''
ON CONFLICT ON CONSTRAINT uq_subscriber_custom_fields_subscriber_key DO NOTHING;

INSERT INTO subscriber_custom_fields (
    id, subscriber_id, key, value_type, value_text, is_active, created_at, updated_at
)
SELECT
    gen_random_uuid(),
    map.subscriber_id,
    'contact_person',
    'string',
    ci.contact_person,
    true,
    NOW(),
    NOW()
FROM splynx_staging.splynx_customer_info ci
JOIN splynx_staging.map_customers map ON map.splynx_customer_id = ci.customer_id
WHERE ci.contact_person IS NOT NULL AND btrim(ci.contact_person) <> ''
ON CONFLICT ON CONSTRAINT uq_subscriber_custom_fields_subscriber_key DO NOTHING;

COMMIT;
