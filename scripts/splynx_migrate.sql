-- One-time migration from splynx_staging into dotmac_sub core tables.
-- Requires splynx_staging tables to be populated (see scripts/splynx_staging.sql).
--
-- Target model: Unified Subscriber (no more people/subscriber_accounts split).
-- Payments use PaymentAllocation many-to-many (no direct invoice_id on payments).
-- Includes: partners → organizations, locations → pop_sites, routers → nas_devices,
--           billing_transactions → ledger_entries.
--
-- QUALIFIED IMPORT: Only customers with at least one subscription, invoice, or
-- payment are imported (~5,028 of ~25,894). Leads/junk are skipped.
-- Internal numbering (SUB-NNNNNN, ACC-NNNNNN, INV-NNNNNN) replaces Splynx logins.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================================
-- MAPPING TABLES (Splynx integer ID → DotMac Sub UUID)
-- ============================================================================

CREATE TABLE IF NOT EXISTS splynx_staging.map_customers (
    splynx_customer_id integer PRIMARY KEY,
    subscriber_id uuid NOT NULL
);

CREATE TABLE IF NOT EXISTS splynx_staging.map_tariffs (
    splynx_tariff_id integer PRIMARY KEY,
    offer_id uuid NOT NULL,
    offer_price_id uuid NOT NULL
);

CREATE TABLE IF NOT EXISTS splynx_staging.map_services (
    splynx_service_id integer PRIMARY KEY,
    subscription_id uuid NOT NULL,
    cpe_id uuid NOT NULL
);

CREATE TABLE IF NOT EXISTS splynx_staging.map_invoices (
    splynx_invoice_id integer PRIMARY KEY,
    invoice_id uuid NOT NULL
);

CREATE TABLE IF NOT EXISTS splynx_staging.map_payments (
    splynx_payment_id integer PRIMARY KEY,
    payment_id uuid NOT NULL
);

CREATE TABLE IF NOT EXISTS splynx_staging.map_routers (
    splynx_router_id integer PRIMARY KEY,
    nas_device_id uuid NOT NULL,
    pop_site_id uuid
);

CREATE TABLE IF NOT EXISTS splynx_staging.map_partners (
    splynx_partner_id integer PRIMARY KEY,
    organization_id uuid NOT NULL
);

CREATE TABLE IF NOT EXISTS splynx_staging.map_locations (
    splynx_location_id integer PRIMARY KEY,
    pop_site_id uuid NOT NULL
);

CREATE TABLE IF NOT EXISTS splynx_staging.map_ipv4 (
    address text PRIMARY KEY,
    ipv4_id uuid NOT NULL
);

CREATE TABLE IF NOT EXISTS splynx_staging.map_ipv6 (
    address text PRIMARY KEY,
    ipv6_id uuid NOT NULL
);

CREATE TABLE IF NOT EXISTS splynx_staging.map_admins (
    splynx_admin_id integer PRIMARY KEY,
    subscriber_id uuid NOT NULL,
    credential_id uuid NOT NULL
);

CREATE TABLE IF NOT EXISTS splynx_staging.map_monitoring (
    splynx_monitoring_id integer PRIMARY KEY,
    network_device_id uuid NOT NULL
);

-- ============================================================================
-- SEED MAPPINGS (generate UUIDs once for deterministic re-runs)
-- ============================================================================

-- QUALIFIED FILTER: only import customers with service, invoice, or payment history
INSERT INTO splynx_staging.map_customers (splynx_customer_id, subscriber_id)
SELECT c.id, gen_random_uuid()
FROM splynx_staging.splynx_customers c
WHERE NOT COALESCE(c.deleted, false)
  AND (
    EXISTS (SELECT 1 FROM splynx_staging.splynx_services_internet s
            WHERE s.customer_id = c.id AND NOT COALESCE(s.deleted, false))
    OR EXISTS (SELECT 1 FROM splynx_staging.splynx_invoices i
              WHERE i.customer_id = c.id AND NOT COALESCE(i.deleted, false))
    OR EXISTS (SELECT 1 FROM splynx_staging.splynx_payments p
              WHERE p.customer_id = c.id AND NOT COALESCE(p.deleted, false))
  )
ON CONFLICT DO NOTHING;

INSERT INTO splynx_staging.map_tariffs (splynx_tariff_id, offer_id, offer_price_id)
SELECT id, gen_random_uuid(), gen_random_uuid()
FROM splynx_staging.splynx_tariffs_internet
ON CONFLICT DO NOTHING;

INSERT INTO splynx_staging.map_services (splynx_service_id, subscription_id, cpe_id)
SELECT s.id, gen_random_uuid(), gen_random_uuid()
FROM splynx_staging.splynx_services_internet s
WHERE EXISTS (SELECT 1 FROM splynx_staging.map_customers mc WHERE mc.splynx_customer_id = s.customer_id)
ON CONFLICT DO NOTHING;

INSERT INTO splynx_staging.map_invoices (splynx_invoice_id, invoice_id)
SELECT i.id, gen_random_uuid()
FROM splynx_staging.splynx_invoices i
WHERE EXISTS (SELECT 1 FROM splynx_staging.map_customers mc WHERE mc.splynx_customer_id = i.customer_id)
ON CONFLICT DO NOTHING;

INSERT INTO splynx_staging.map_payments (splynx_payment_id, payment_id)
SELECT p.id, gen_random_uuid()
FROM splynx_staging.splynx_payments p
WHERE EXISTS (SELECT 1 FROM splynx_staging.map_customers mc WHERE mc.splynx_customer_id = p.customer_id)
ON CONFLICT DO NOTHING;

INSERT INTO splynx_staging.map_routers (splynx_router_id, nas_device_id, pop_site_id)
SELECT r.id, gen_random_uuid(),
    CASE WHEN r.location_id > 0 THEN ml.pop_site_id ELSE NULL END
FROM splynx_staging.splynx_routers r
LEFT JOIN splynx_staging.map_locations ml ON ml.splynx_location_id = r.location_id
ON CONFLICT DO NOTHING;

INSERT INTO splynx_staging.map_partners (splynx_partner_id, organization_id)
SELECT id, gen_random_uuid()
FROM splynx_staging.splynx_partners
ON CONFLICT DO NOTHING;

INSERT INTO splynx_staging.map_locations (splynx_location_id, pop_site_id)
SELECT id, gen_random_uuid()
FROM splynx_staging.splynx_locations
ON CONFLICT DO NOTHING;

-- Admin users mapping
INSERT INTO splynx_staging.map_admins (splynx_admin_id, subscriber_id, credential_id)
SELECT id, gen_random_uuid(), gen_random_uuid()
FROM splynx_staging.splynx_admins WHERE NOT COALESCE(deleted, false)
ON CONFLICT DO NOTHING;

-- Monitoring devices mapping
INSERT INTO splynx_staging.map_monitoring (splynx_monitoring_id, network_device_id)
SELECT id, gen_random_uuid()
FROM splynx_staging.splynx_monitoring WHERE NOT COALESCE(deleted, false)
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 1. REFERENCE DATA: Partners → Organizations
-- ============================================================================

INSERT INTO organizations (
    id,
    name,
    created_at,
    updated_at
)
SELECT
    map.organization_id,
    substring(p.name from 1 for 160),
    NOW(),
    NOW()
FROM splynx_staging.splynx_partners p
JOIN splynx_staging.map_partners map ON map.splynx_partner_id = p.id
WHERE NOT COALESCE(p.deleted, false)
ON CONFLICT (id) DO NOTHING;

-- ============================================================================
-- 2. REFERENCE DATA: Locations → Pop Sites
-- ============================================================================

INSERT INTO pop_sites (
    id,
    name,
    code,
    is_active,
    created_at,
    updated_at
)
SELECT
    map.pop_site_id,
    substring(l.name from 1 for 160),
    'splynx_loc_' || l.id::text,
    NOT COALESCE(l.deleted, false),
    NOW(),
    NOW()
FROM splynx_staging.splynx_locations l
JOIN splynx_staging.map_locations map ON map.splynx_location_id = l.id
ON CONFLICT (id) DO NOTHING;

-- ============================================================================
-- 3. SUBSCRIBERS (unified model: identity + account + billing)
--    Internal numbering: SUB-NNNNNN, ACC-NNNNNN (Splynx login preserved later)
-- ============================================================================

WITH customer_data AS (
    SELECT
        c.*,
        map.subscriber_id,
        CASE
            WHEN c.email IS NULL OR btrim(c.email) = '' THEN NULL
            ELSE lower(btrim(c.email))
        END AS email_base,
        -- Sequential row number for internal numbering
        row_number() OVER (ORDER BY c.id) AS rn
    FROM splynx_staging.splynx_customers c
    JOIN splynx_staging.map_customers map ON map.splynx_customer_id = c.id
),
email_dedup AS (
    SELECT
        *,
        row_number() OVER (PARTITION BY email_base ORDER BY id) AS email_seq
    FROM customer_data
),
existing_email AS (
    SELECT email AS existing_email FROM subscribers
)
INSERT INTO subscribers (
    id,
    -- Identity fields
    first_name,
    last_name,
    display_name,
    email,
    email_verified,
    phone,
    gender,
    -- Address fields
    address_line1,
    city,
    postal_code,
    -- Account fields
    subscriber_number,
    account_number,
    account_start_date,
    status,
    is_active,
    marketing_opt_in,
    -- Organization link
    organization_id,
    -- Billing fields (merged from customer_billing)
    billing_enabled,
    billing_name,
    billing_address_line1,
    billing_city,
    billing_postal_code,
    payment_method,
    deposit,
    billing_day,
    payment_due_days,
    grace_period_days,
    min_balance,
    -- Splynx traceability
    splynx_customer_id,
    -- Timestamps
    created_at,
    updated_at
)
SELECT
    ed.subscriber_id,
    -- Identity
    substring(COALESCE(NULLIF(split_part(ed.name, ' ', 1), ''), 'Unknown') from 1 for 80),
    substring(COALESCE(NULLIF(trim(regexp_replace(ed.name, '^[^ ]+\s*', '')), ''), 'Customer') from 1 for 80),
    substring(ed.name from 1 for 120),
    substring(
        CASE
            WHEN ed.email_base IS NULL THEN 'splynx_customer_' || ed.id::text || '@invalid.local'
            WHEN ed.email_base IN (SELECT existing_email FROM existing_email) THEN regexp_replace(ed.email_base, '@', '+splynx' || ed.id::text || '@')
            WHEN ed.email_seq = 1 THEN ed.email_base
            ELSE regexp_replace(ed.email_base, '@', '+' || ed.email_seq::text || '@')
        END
        from 1 for 255
    ),
    false,
    substring(ed.phone from 1 for 40),
    'unknown'::gender,
    -- Address
    substring(ed.street_1 from 1 for 120),
    ed.city,
    substring(ed.zip_code from 1 for 20),
    -- Account: internal numbering (SUB-NNNNNN, ACC-NNNNNN)
    'SUB-' || lpad(ed.rn::text, 6, '0'),
    'ACC-' || lpad(ed.rn::text, 6, '0'),
    COALESCE(ed.date_add::timestamp with time zone, NOW()),
    CASE
        WHEN COALESCE(ed.deleted, false) THEN 'canceled'
        WHEN ed.status IN ('disabled', 'blocked') THEN 'suspended'
        ELSE 'active'
    END::subscriberstatus,
    NOT COALESCE(ed.deleted, false),
    false,
    -- Organization (from partner)
    mp.organization_id,
    -- Billing (from customer_billing)
    COALESCE(b.enabled, true),
    b.billing_person,
    substring(b.billing_street_1 from 1 for 160),
    b.billing_city,
    substring(b.billing_zip_code from 1 for 20),
    b.payment_method,
    b.deposit,
    b.billing_date,
    b.billing_due,
    b.grace_period,
    b.min_balance,
    -- Splynx traceability
    ed.id,
    -- Timestamps
    COALESCE(ed.date_add::timestamp with time zone, NOW()),
    COALESCE(ed.last_update, NOW())
FROM email_dedup ed
LEFT JOIN splynx_staging.splynx_customer_billing b ON b.customer_id = ed.id
LEFT JOIN splynx_staging.map_partners mp ON mp.splynx_partner_id = ed.partner_id
ON CONFLICT (id) DO NOTHING;

-- ============================================================================
-- 4. SERVICE ADDRESSES (from customer data)
-- ============================================================================

INSERT INTO addresses (
    id,
    subscriber_id,
    address_type,
    address_line1,
    city,
    postal_code,
    latitude,
    longitude,
    is_primary,
    created_at,
    updated_at
)
SELECT
    gen_random_uuid(),
    map.subscriber_id,
    'service'::addresstype,
    substring(c.street_1 from 1 for 120),
    c.city,
    substring(c.zip_code from 1 for 20),
    NULLIF(trim(split_part(c.gps, ',', 1)), '')::double precision,
    NULLIF(trim(split_part(c.gps, ',', 2)), '')::double precision,
    true,
    NOW(),
    NOW()
FROM splynx_staging.splynx_customers c
JOIN splynx_staging.map_customers map ON map.splynx_customer_id = c.id
WHERE c.street_1 IS NOT NULL AND btrim(c.street_1) <> ''
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 5. NAS DEVICES (from routers)
-- ============================================================================

-- Re-seed router mappings now that locations are mapped
UPDATE splynx_staging.map_routers mr
SET pop_site_id = ml.pop_site_id
FROM splynx_staging.splynx_routers r
JOIN splynx_staging.map_locations ml ON ml.splynx_location_id = r.location_id
WHERE mr.splynx_router_id = r.id AND r.location_id > 0;

INSERT INTO nas_devices (
    id,
    name,
    code,
    vendor,
    model,
    -- Network
    ip_address,
    management_ip,
    nas_ip,
    -- RADIUS
    shared_secret,
    -- Location
    pop_site_id,
    -- Defaults for NOT NULL columns
    api_verify_tls,
    ssh_verify_host_key,
    backup_enabled,
    current_subscriber_count,
    health_status,
    -- Status
    status,
    is_active,
    -- Timestamps
    created_at,
    updated_at
)
SELECT
    map.nas_device_id,
    substring(r.title from 1 for 160),
    'splynx_router_' || r.id::text,
    CASE
        WHEN lower(r.model) LIKE '%mikrotik%' OR r.nas_type = 1 THEN 'mikrotik'
        WHEN lower(r.model) LIKE '%huawei%' THEN 'huawei'
        WHEN lower(r.model) LIKE '%ubiquiti%' OR lower(r.model) LIKE '%ubnt%' THEN 'ubiquiti'
        WHEN lower(r.model) LIKE '%cisco%' THEN 'cisco'
        WHEN lower(r.model) LIKE '%cambium%' THEN 'cambium'
        ELSE 'other'
    END::nasvendor,
    substring(r.model from 1 for 120),
    -- Network
    r.ip,
    r.ip,
    COALESCE(NULLIF(r.nas_ip, ''), r.ip),
    -- RADIUS secret
    r.radius_secret,
    -- Location
    map.pop_site_id,
    -- Defaults for NOT NULL columns
    false,   -- api_verify_tls
    false,   -- ssh_verify_host_key
    false,   -- backup_enabled
    0,       -- current_subscriber_count
    'unknown', -- health_status
    -- Status
    CASE WHEN COALESCE(r.deleted, false) THEN 'decommissioned' ELSE 'active' END::nasdevicestatus,
    NOT COALESCE(r.deleted, false),
    NOW(),
    NOW()
FROM splynx_staging.splynx_routers r
JOIN splynx_staging.map_routers map ON map.splynx_router_id = r.id
ON CONFLICT (id) DO NOTHING;

-- ============================================================================
-- 6. CATALOG OFFERS (from tariffs)
-- ============================================================================

INSERT INTO catalog_offers (
    id,
    name,
    code,
    service_type,
    access_type,
    billing_mode,
    price_basis,
    billing_cycle,
    contract_term,
    splynx_tariff_id,
    splynx_service_name,
    splynx_tax_id,
    with_vat,
    vat_percent,
    speed_download_mbps,
    speed_upload_mbps,
    aggregation,
    priority,
    available_for_services,
    show_on_customer_portal,
    guaranteed_speed,
    status,
    description,
    is_active,
    created_at,
    updated_at
)
SELECT
    map.offer_id,
    substring(t.title from 1 for 120),
    'splynx_tariff_' || t.id::text,
    'residential'::servicetype,
    'fixed_wireless'::accesstype,
    'prepaid'::billingmode,
    'flat'::pricebasis,
    'monthly'::billingcycle,
    'month_to_month'::contractterm,
    t.id,
    substring(t.service_name from 1 for 160),
    t.tax_id,
    COALESCE(t.with_vat, false),
    t.vat_percent,
    t.speed_download,
    t.speed_upload,
    t.aggregation,
    t.priority,
    COALESCE(t.available_for_services, true),
    COALESCE(t.show_on_customer_portal, true),
    'none'::guaranteedspeedtype,
    CASE WHEN COALESCE(t.deleted, false) THEN 'archived'::offerstatus ELSE 'active'::offerstatus END,
    t.service_name,
    NOT COALESCE(t.deleted, false),
    NOW(),
    NOW()
FROM splynx_staging.splynx_tariffs_internet t
JOIN splynx_staging.map_tariffs map ON map.splynx_tariff_id = t.id
ON CONFLICT (id) DO NOTHING;

INSERT INTO offer_prices (
    id,
    offer_id,
    price_type,
    amount,
    currency,
    billing_cycle,
    unit,
    description,
    is_active,
    created_at,
    updated_at
)
SELECT
    map.offer_price_id,
    map.offer_id,
    'recurring'::pricetype,
    COALESCE(t.price, 0),
    'NGN',
    'monthly'::billingcycle,
    'month'::priceunit,
    t.title,
    NOT COALESCE(t.deleted, false),
    NOW(),
    NOW()
FROM splynx_staging.splynx_tariffs_internet t
JOIN splynx_staging.map_tariffs map ON map.splynx_tariff_id = t.id
ON CONFLICT (id) DO NOTHING;

-- ============================================================================
-- 7. SUBSCRIPTIONS (from services_internet)
-- ============================================================================

INSERT INTO subscriptions (
    id,
    subscriber_id,
    offer_id,
    offer_version_id,
    service_address_id,
    status,
    billing_mode,
    contract_term,
    start_at,
    end_at,
    splynx_service_id,
    router_id,
    service_description,
    quantity,
    unit,
    unit_price,
    discount,
    discount_value,
    discount_type,
    service_status_raw,
    login,
    ipv4_address,
    ipv6_address,
    mac_address,
    -- Link to NAS device if router maps
    provisioning_nas_device_id,
    created_at,
    updated_at
)
SELECT
    map.subscription_id,
    cust_map.subscriber_id,
    tariff_map.offer_id,
    NULL,
    NULL,
    CASE
        WHEN s.status = 'active' THEN 'active'
        WHEN s.status IN ('disabled', 'hidden') THEN 'suspended'
        WHEN s.status = 'stopped' THEN 'canceled'
        ELSE 'pending'
    END::subscriptionstatus,
    'prepaid'::billingmode,
    'month_to_month'::contractterm,
    s.start_date,
    s.end_date,
    s.id,
    s.router_id,
    s.description,
    s.quantity,
    s.unit,
    s.unit_price,
    COALESCE(s.discount, false),
    s.discount_value,
    s.discount_type,
    s.status,
    s.login,
    s.ipv4,
    s.ipv6,
    s.mac,
    -- Link to migrated NAS device
    router_map.nas_device_id,
    NOW(),
    NOW()
FROM splynx_staging.splynx_services_internet s
JOIN splynx_staging.map_services map ON map.splynx_service_id = s.id
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = s.customer_id
JOIN splynx_staging.map_tariffs tariff_map ON tariff_map.splynx_tariff_id = s.tariff_id
LEFT JOIN splynx_staging.map_routers router_map ON router_map.splynx_router_id = s.router_id
ON CONFLICT (id) DO NOTHING;

-- CPE devices from service MAC addresses
INSERT INTO cpe_devices (
    id,
    subscriber_id,
    subscription_id,
    device_type,
    status,
    mac_address,
    installed_at,
    notes,
    created_at,
    updated_at
)
SELECT
    map.cpe_id,
    cust_map.subscriber_id,
    map.subscription_id,
    'router'::devicetype,
    'active'::cpe_devicestatus,
    s.mac,
    s.start_date,
    s.login,
    NOW(),
    NOW()
FROM splynx_staging.splynx_services_internet s
JOIN splynx_staging.map_services map ON map.splynx_service_id = s.id
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = s.customer_id
WHERE s.mac IS NOT NULL AND btrim(s.mac) <> ''
ON CONFLICT (id) DO NOTHING;

-- ============================================================================
-- 8. IP ADDRESSES + ASSIGNMENTS
-- ============================================================================

INSERT INTO splynx_staging.map_ipv4 (address, ipv4_id)
SELECT DISTINCT s.ipv4, gen_random_uuid()
FROM splynx_staging.splynx_services_internet s
WHERE s.ipv4 IS NOT NULL AND btrim(s.ipv4) <> ''
ON CONFLICT DO NOTHING;

INSERT INTO splynx_staging.map_ipv6 (address, ipv6_id)
SELECT DISTINCT s.ipv6, gen_random_uuid()
FROM splynx_staging.splynx_services_internet s
WHERE s.ipv6 IS NOT NULL AND btrim(s.ipv6) <> ''
ON CONFLICT DO NOTHING;

INSERT INTO ipv4_addresses (id, address, is_reserved, created_at, updated_at)
SELECT map.ipv4_id, map.address, false, NOW(), NOW()
FROM splynx_staging.map_ipv4 map
ON CONFLICT (id) DO NOTHING;

INSERT INTO ipv6_addresses (id, address, is_reserved, created_at, updated_at)
SELECT map.ipv6_id, map.address, false, NOW(), NOW()
FROM splynx_staging.map_ipv6 map
ON CONFLICT (id) DO NOTHING;

-- IPv4 assignments
INSERT INTO ip_assignments (
    id, subscriber_id, subscription_id, ip_version,
    ipv4_address_id, ipv6_address_id, is_active, created_at, updated_at
)
SELECT DISTINCT ON (v4.ipv4_id)
    gen_random_uuid(),
    cust_map.subscriber_id,
    service_map.subscription_id,
    'ipv4'::ipversion,
    v4.ipv4_id,
    NULL,
    true,
    NOW(),
    NOW()
FROM splynx_staging.splynx_services_internet s
JOIN splynx_staging.map_services service_map ON service_map.splynx_service_id = s.id
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = s.customer_id
JOIN splynx_staging.map_ipv4 v4 ON v4.address = s.ipv4
WHERE s.ipv4 IS NOT NULL AND btrim(s.ipv4) <> ''
ORDER BY v4.ipv4_id, s.id
ON CONFLICT (ipv4_address_id) DO NOTHING;

-- IPv6 assignments (only where no IPv4)
INSERT INTO ip_assignments (
    id, subscriber_id, subscription_id, ip_version,
    ipv4_address_id, ipv6_address_id, is_active, created_at, updated_at
)
SELECT DISTINCT ON (v6.ipv6_id)
    gen_random_uuid(),
    cust_map.subscriber_id,
    service_map.subscription_id,
    'ipv6'::ipversion,
    NULL,
    v6.ipv6_id,
    true,
    NOW(),
    NOW()
FROM splynx_staging.splynx_services_internet s
JOIN splynx_staging.map_services service_map ON service_map.splynx_service_id = s.id
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = s.customer_id
JOIN splynx_staging.map_ipv6 v6 ON v6.address = s.ipv6
WHERE (s.ipv4 IS NULL OR btrim(s.ipv4) = '') AND s.ipv6 IS NOT NULL AND btrim(s.ipv6) <> ''
ORDER BY v6.ipv6_id, s.id
ON CONFLICT (ipv6_address_id) DO NOTHING;

-- ============================================================================
-- 9. INVOICES + LINES (with internal numbering and new columns)
-- ============================================================================

INSERT INTO invoices (
    id,
    account_id,
    invoice_number,
    status,
    currency,
    subtotal,
    tax_total,
    total,
    balance_due,
    billing_period_start,
    billing_period_end,
    issued_at,
    due_at,
    paid_at,
    memo,
    is_sent,
    added_by_id,
    splynx_invoice_id,
    is_active,
    created_at,
    updated_at
)
SELECT
    map.invoice_id,
    cust_map.subscriber_id,
    -- Internal numbering: INV-NNNNNN
    'INV-' || lpad((row_number() OVER (ORDER BY i.id))::text, 6, '0'),
    CASE
        WHEN i.status = 'paid' THEN 'paid'
        WHEN i.status = 'not_paid' AND i.due > 0 THEN 'issued'
        WHEN i.status = 'not_paid' AND i.due <= 0 THEN 'paid'
        ELSE 'void'
    END::invoicestatus,
    'NGN',
    COALESCE(i.total, 0),
    0,
    COALESCE(i.total, 0),
    GREATEST(COALESCE(i.due, 0), 0),
    -- Billing period from invoice dates
    COALESCE(i.date_created::timestamp with time zone, i.real_create_datetime),
    i.date_till::timestamp with time zone,
    -- Issued/due/paid
    COALESCE(i.real_create_datetime, i.date_created::timestamp with time zone),
    i.date_till::timestamp with time zone,
    CASE WHEN i.status = 'paid' THEN COALESCE(i.date_payment::timestamp with time zone, i.date_updated::timestamp with time zone) ELSE NULL END,
    COALESCE(i.memo, i.note),
    -- New columns
    COALESCE(i.is_sent, false),
    admin_sub.id,  -- added_by_id: only set if admin subscriber already exists
    i.id,
    NOT COALESCE(i.deleted, false),
    NOW(),
    NOW()
FROM splynx_staging.splynx_invoices i
JOIN splynx_staging.map_invoices map ON map.splynx_invoice_id = i.id
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = i.customer_id
LEFT JOIN splynx_staging.map_admins admin_map
    ON admin_map.splynx_admin_id = i.added_by_id
LEFT JOIN subscribers admin_sub ON admin_sub.id = admin_map.subscriber_id
ON CONFLICT (id) DO NOTHING;

INSERT INTO invoice_lines (
    id,
    invoice_id,
    description,
    quantity,
    unit_price,
    amount,
    tax_application,
    is_active,
    created_at,
    updated_at
)
SELECT
    gen_random_uuid(),
    inv_map.invoice_id,
    COALESCE(li.description, ''),
    COALESCE(li.quantity, 1),
    COALESCE(li.price, 0),
    COALESCE(li.quantity * li.price, 0),
    'exclusive'::taxapplication,
    NOT COALESCE(li.deleted, false),
    NOW(),
    NOW()
FROM splynx_staging.splynx_invoice_items li
JOIN splynx_staging.map_invoices inv_map ON inv_map.splynx_invoice_id = li.invoice_id
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 10. PAYMENTS (with receipt_number and splynx_payment_id)
-- ============================================================================

INSERT INTO payments (
    id,
    account_id,
    amount,
    currency,
    status,
    paid_at,
    external_id,
    memo,
    receipt_number,
    splynx_payment_id,
    is_active,
    created_at,
    updated_at
)
SELECT
    map.payment_id,
    cust_map.subscriber_id,
    COALESCE(p.amount, 0),
    'NGN',
    'succeeded'::paymentstatus,
    COALESCE(p.real_create_datetime, p.payment_date::timestamp with time zone),
    COALESCE(p.receipt_number, p.transaction_id::text),
    COALESCE(p.memo, p.note, p.comment),
    -- New columns
    COALESCE(p.receipt_number, p.transaction_id::text),
    p.id,
    NOT COALESCE(p.deleted, false),
    NOW(),
    NOW()
FROM splynx_staging.splynx_payments p
JOIN splynx_staging.map_payments map ON map.splynx_payment_id = p.id
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = p.customer_id
ON CONFLICT (id) DO NOTHING;

-- ============================================================================
-- 11. PAYMENT ALLOCATIONS (payment ↔ invoice many-to-many)
-- ============================================================================

INSERT INTO payment_allocations (
    id,
    payment_id,
    invoice_id,
    amount,
    memo,
    created_at
)
SELECT
    gen_random_uuid(),
    pay_map.payment_id,
    inv_map.invoice_id,
    COALESCE(p.amount, 0),
    'Migrated from Splynx payment #' || p.id::text,
    NOW()
FROM splynx_staging.splynx_payments p
JOIN splynx_staging.map_payments pay_map ON pay_map.splynx_payment_id = p.id
JOIN splynx_staging.map_invoices inv_map ON inv_map.splynx_invoice_id = p.invoice_id
WHERE p.invoice_id IS NOT NULL AND p.invoice_id > 0
    AND NOT COALESCE(p.deleted, false)
ON CONFLICT ON CONSTRAINT uq_payment_allocations_payment_invoice DO NOTHING;

-- ============================================================================
-- 12. LEDGER ENTRIES (from billing_transactions)
-- ============================================================================

INSERT INTO ledger_entries (
    id,
    account_id,
    invoice_id,
    payment_id,
    entry_type,
    source,
    amount,
    currency,
    memo,
    is_active,
    created_at
)
SELECT
    gen_random_uuid(),
    cust_map.subscriber_id,
    inv_map.invoice_id,
    pay_map.payment_id,
    -- Splynx type: debit increases balance owed, credit reduces it
    CASE
        WHEN bt.type = 'debit' THEN 'debit'::ledgerentrytype
        WHEN bt.type = 'credit' THEN 'credit'::ledgerentrytype
        ELSE 'debit'::ledgerentrytype
    END,
    -- Map Splynx source to ledger source
    CASE
        WHEN bt.invoice_id IS NOT NULL AND bt.invoice_id > 0 THEN 'invoice'::ledgersource
        WHEN bt.payment_id IS NOT NULL AND bt.payment_id > 0 THEN 'payment'::ledgersource
        WHEN bt.credit_note_id IS NOT NULL AND bt.credit_note_id > 0 THEN 'credit_note'::ledgersource
        WHEN bt.source = 'manual' THEN 'adjustment'::ledgersource
        ELSE 'other'::ledgersource
    END,
    COALESCE(bt.total, 0),
    'NGN',
    COALESCE(
        NULLIF(bt.description, ''),
        NULLIF(bt.comment, ''),
        'Splynx transaction #' || bt.id::text
    ),
    NOT COALESCE(bt.deleted, false),
    COALESCE(bt.date::timestamp with time zone, NOW())
FROM splynx_staging.splynx_billing_transactions bt
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = bt.customer_id
LEFT JOIN splynx_staging.map_invoices inv_map ON inv_map.splynx_invoice_id = bt.invoice_id
LEFT JOIN splynx_staging.map_payments pay_map ON pay_map.splynx_payment_id = bt.payment_id
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 13. LEDGER ENTRIES for payments (credit entries for each allocation)
-- ============================================================================

INSERT INTO ledger_entries (
    id,
    account_id,
    invoice_id,
    payment_id,
    entry_type,
    source,
    amount,
    currency,
    memo,
    is_active,
    created_at
)
SELECT
    gen_random_uuid(),
    cust_map.subscriber_id,
    inv_map.invoice_id,
    pay_map.payment_id,
    'credit'::ledgerentrytype,
    'payment'::ledgersource,
    COALESCE(p.amount, 0),
    'NGN',
    'Payment allocation from Splynx payment #' || p.id::text,
    NOT COALESCE(p.deleted, false),
    COALESCE(p.real_create_datetime, p.payment_date::timestamp with time zone, NOW())
FROM splynx_staging.splynx_payments p
JOIN splynx_staging.map_payments pay_map ON pay_map.splynx_payment_id = p.id
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = p.customer_id
LEFT JOIN splynx_staging.map_invoices inv_map ON inv_map.splynx_invoice_id = p.invoice_id
WHERE NOT COALESCE(p.deleted, false)
    -- Only if not already captured from billing_transactions
    AND NOT EXISTS (
        SELECT 1 FROM splynx_staging.splynx_billing_transactions bt
        WHERE bt.payment_id = p.id AND bt.customer_id = p.customer_id
    )
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 14. LEDGER ENTRIES for invoices (debit entries)
-- ============================================================================

INSERT INTO ledger_entries (
    id,
    account_id,
    invoice_id,
    payment_id,
    entry_type,
    source,
    amount,
    currency,
    memo,
    is_active,
    created_at
)
SELECT
    gen_random_uuid(),
    cust_map.subscriber_id,
    inv_map.invoice_id,
    NULL,
    'debit'::ledgerentrytype,
    'invoice'::ledgersource,
    COALESCE(i.total, 0),
    'NGN',
    'Invoice ' || i.number || ' from Splynx',
    NOT COALESCE(i.deleted, false),
    COALESCE(i.real_create_datetime, i.date_created::timestamp with time zone, NOW())
FROM splynx_staging.splynx_invoices i
JOIN splynx_staging.map_invoices inv_map ON inv_map.splynx_invoice_id = i.id
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = i.customer_id
WHERE NOT COALESCE(i.deleted, false)
    -- Only if not already captured from billing_transactions
    AND NOT EXISTS (
        SELECT 1 FROM splynx_staging.splynx_billing_transactions bt
        WHERE bt.invoice_id = i.id AND bt.customer_id = i.customer_id
    )
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 15. CUSTOMER CUSTOM FIELDS (from customers_values)
-- ============================================================================
-- splynx_customers_values.id = customer_id, name = field_name, value = field_value
-- Active field names: zoho_id, social_id, splynx_addon_resellers_reseller

-- Zoho IDs (skip deleted/prefixed values)
INSERT INTO subscriber_custom_fields (
    id, subscriber_id, key, value_type, value_text, is_active, created_at, updated_at
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
JOIN splynx_staging.map_customers map ON map.splynx_customer_id = cv.id
WHERE cv.name = 'zoho_id'
    AND cv.value IS NOT NULL AND btrim(cv.value) <> ''
    AND cv.value NOT LIKE '%deleted%'
ON CONFLICT ON CONSTRAINT uq_subscriber_custom_fields_subscriber_key DO NOTHING;

-- Social IDs (skip deleted/prefixed values)
INSERT INTO subscriber_custom_fields (
    id, subscriber_id, key, value_type, value_text, is_active, created_at, updated_at
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
JOIN splynx_staging.map_customers map ON map.splynx_customer_id = cv.id
WHERE cv.name = 'social_id'
    AND cv.value IS NOT NULL AND btrim(cv.value) <> ''
    AND cv.value NOT LIKE '%deleted%'
ON CONFLICT ON CONSTRAINT uq_subscriber_custom_fields_subscriber_key DO NOTHING;

-- Reseller association
INSERT INTO subscriber_custom_fields (
    id, subscriber_id, key, value_type, value_text, is_active, created_at, updated_at
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
JOIN splynx_staging.map_customers map ON map.splynx_customer_id = cv.id
WHERE cv.name = 'splynx_addon_resellers_reseller'
    AND cv.value IS NOT NULL AND btrim(cv.value) <> ''
ON CONFLICT ON CONSTRAINT uq_subscriber_custom_fields_subscriber_key DO NOTHING;

-- ============================================================================
-- 16. EXTERNAL REFERENCES (from accounting_customers → Zoho links)
-- ============================================================================

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
JOIN splynx_staging.map_customers map ON map.splynx_customer_id = ac.customer_id
WHERE ac.accounting_id IS NOT NULL AND btrim(ac.accounting_id) <> ''
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 17. INVENTORY ITEMS → CPE DEVICES (enrich with serial numbers + barcodes)
-- ============================================================================
-- Inventory items assigned to customers map to CPE devices.
-- Items with barcodes likely represent network equipment (APs, routers).

CREATE TABLE IF NOT EXISTS splynx_staging.map_inventory (
    splynx_inventory_id integer PRIMARY KEY,
    cpe_id uuid NOT NULL
);

INSERT INTO splynx_staging.map_inventory (splynx_inventory_id, cpe_id)
SELECT id, gen_random_uuid()
FROM splynx_staging.splynx_inventory_items
WHERE status = 'assigned' AND NOT COALESCE(deleted, false)
    AND customer_id IS NOT NULL AND customer_id > 0
ON CONFLICT DO NOTHING;

INSERT INTO cpe_devices (
    id,
    subscriber_id,
    subscription_id,
    device_type,
    status,
    serial_number,
    mac_address,
    notes,
    created_at,
    updated_at
)
SELECT
    inv_map.cpe_id,
    cust_map.subscriber_id,
    -- Link to subscription if service_id maps
    svc_map.subscription_id,
    'access_point'::devicetype,
    CASE
        WHEN i.status = 'assigned' THEN 'active'
        ELSE 'inactive'
    END::cpe_devicestatus,
    NULLIF(btrim(i.serial_number), ''),
    -- Use barcode as MAC if it looks like a MAC (12 hex chars)
    CASE
        WHEN length(btrim(i.barcode)) = 12
             AND btrim(i.barcode) ~ '^[0-9a-fA-F]+$'
        THEN upper(
            substring(btrim(i.barcode) from 1 for 2) || ':' ||
            substring(btrim(i.barcode) from 3 for 2) || ':' ||
            substring(btrim(i.barcode) from 5 for 2) || ':' ||
            substring(btrim(i.barcode) from 7 for 2) || ':' ||
            substring(btrim(i.barcode) from 9 for 2) || ':' ||
            substring(btrim(i.barcode) from 11 for 2)
        )
        ELSE NULL
    END,
    COALESCE(
        NULLIF(btrim(i.notes), ''),
        'Splynx inventory #' || i.id::text
            || CASE WHEN i.barcode IS NOT NULL AND btrim(i.barcode) <> ''
                    THEN ' barcode=' || i.barcode ELSE '' END
    ),
    COALESCE(i.updated_at, NOW()),
    NOW()
FROM splynx_staging.splynx_inventory_items i
JOIN splynx_staging.map_inventory inv_map ON inv_map.splynx_inventory_id = i.id
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = i.customer_id
LEFT JOIN splynx_staging.map_services svc_map ON svc_map.splynx_service_id = i.service_id
ON CONFLICT (id) DO NOTHING;

-- ============================================================================
-- 18. IPv4 ADDRESSES (from ipv4_networks_ip — proper pool data)
-- ============================================================================
-- This enriches the IP address data with proper network/pool associations.
-- Addresses already imported from services_internet are left untouched.

INSERT INTO splynx_staging.map_ipv4 (address, ipv4_id)
SELECT DISTINCT ip, gen_random_uuid()
FROM splynx_staging.splynx_ipv4_networks_ip
WHERE NOT COALESCE(deleted, false)
    AND ip IS NOT NULL AND btrim(ip) <> ''
ON CONFLICT DO NOTHING;

-- Insert any new IPv4 addresses not already present from service migration
INSERT INTO ipv4_addresses (id, address, is_reserved, created_at, updated_at)
SELECT m.ipv4_id, m.address, false, NOW(), NOW()
FROM splynx_staging.map_ipv4 m
ON CONFLICT (id) DO NOTHING;

-- Create IP assignments for network IPs that are marked as used
-- and linked to a customer (but not already assigned from services)
INSERT INTO ip_assignments (
    id, subscriber_id, subscription_id, ip_version,
    ipv4_address_id, ipv6_address_id, is_active, created_at, updated_at
)
SELECT DISTINCT ON (v4.ipv4_id)
    gen_random_uuid(),
    cust_map.subscriber_id,
    NULL,
    'ipv4'::ipversion,
    v4.ipv4_id,
    NULL,
    nip.is_used,
    NOW(),
    NOW()
FROM splynx_staging.splynx_ipv4_networks_ip nip
JOIN splynx_staging.map_ipv4 v4 ON v4.address = nip.ip
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = nip.customer_id
WHERE NOT COALESCE(nip.deleted, false)
    AND nip.customer_id > 0
    -- Skip if already assigned from service migration
    AND NOT EXISTS (
        SELECT 1 FROM ip_assignments ia
        WHERE ia.ipv4_address_id = v4.ipv4_id
    )
ORDER BY v4.ipv4_id, nip.id
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 19. MRR STATISTICS → KPI AGGREGATES
-- ============================================================================
-- Monthly recurring revenue per customer, aggregated by date.
-- Maps to kpi_aggregates for revenue reporting.

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
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 20. ADMIN USERS IMPORT (Splynx admins → Subscriber + UserCredential + Role)
-- ============================================================================
-- Splynx admin users are imported as Subscribers with admin role.
-- Their bcrypt password hashes are preserved (passlib CryptContext supports them).

-- Step 1: Insert admin users as Subscribers (ADM-NNNNNN numbering)
WITH admin_data AS (
    SELECT
        a.*,
        map.subscriber_id,
        row_number() OVER (ORDER BY a.id) AS rn
    FROM splynx_staging.splynx_admins a
    JOIN splynx_staging.map_admins map ON map.splynx_admin_id = a.id
),
existing_email AS (
    SELECT email AS existing_email FROM subscribers
)
INSERT INTO subscribers (
    id,
    first_name,
    last_name,
    display_name,
    email,
    email_verified,
    phone,
    gender,
    subscriber_number,
    account_number,
    status,
    is_active,
    marketing_opt_in,
    billing_enabled,
    notes,
    created_at,
    updated_at
)
SELECT
    ad.subscriber_id,
    substring(COALESCE(NULLIF(split_part(ad.name, ' ', 1), ''), ad.login, 'Admin') from 1 for 80),
    substring(COALESCE(NULLIF(trim(regexp_replace(ad.name, '^[^ ]+\s*', '')), ''), 'User') from 1 for 80),
    substring(COALESCE(NULLIF(ad.name, ''), ad.login) from 1 for 120),
    -- Email collision guard: append +admin<id> if email already exists
    substring(
        CASE
            WHEN ad.email IS NULL OR btrim(ad.email) = '' THEN 'splynx_admin_' || ad.id::text || '@invalid.local'
            WHEN lower(btrim(ad.email)) IN (SELECT existing_email FROM existing_email) THEN regexp_replace(lower(btrim(ad.email)), '@', '+admin' || ad.id::text || '@')
            ELSE lower(btrim(ad.email))
        END
        from 1 for 255
    ),
    true,
    substring(ad.phone from 1 for 40),
    'unknown'::gender,
    'ADM-' || lpad(ad.rn::text, 6, '0'),
    'ADM-' || lpad(ad.rn::text, 6, '0'),
    'active'::subscriberstatus,
    NOT COALESCE(ad.deleted, false),
    false,
    false,   -- billing_enabled (admin users don't have billing)
    'Imported from Splynx admin #' || ad.id::text,
    COALESCE(ad.updated_at, NOW()),
    COALESCE(ad.updated_at, NOW())
FROM admin_data ad
ON CONFLICT (id) DO NOTHING;

-- Step 2: Create UserCredential for each admin (preserve bcrypt hashes)
INSERT INTO user_credentials (
    id,
    subscriber_id,
    provider,
    username,
    password_hash,
    failed_login_attempts,
    must_change_password,
    is_active,
    created_at,
    updated_at
)
SELECT
    map.credential_id,
    map.subscriber_id,
    'local'::authprovider,
    a.login,
    '!needs_reset',  -- No password in Splynx admin export; force reset on first login
    0,                -- failed_login_attempts
    true,             -- Force password change on first login
    NOT COALESCE(a.deleted, false),
    NOW(),
    NOW()
FROM splynx_staging.splynx_admins a
JOIN splynx_staging.map_admins map ON map.splynx_admin_id = a.id
WHERE a.login IS NOT NULL AND btrim(a.login) <> ''
ON CONFLICT (id) DO NOTHING;

-- Step 3: Assign 'admin' role to all imported admins
INSERT INTO subscriber_roles (
    id,
    subscriber_id,
    role_id,
    assigned_at
)
SELECT
    gen_random_uuid(),
    map.subscriber_id,
    r.id,
    NOW()
FROM splynx_staging.map_admins map
JOIN roles r ON r.name = 'admin'
ON CONFLICT ON CONSTRAINT uq_subscriber_roles_subscriber_role DO NOTHING;

-- ============================================================================
-- 21. MONITORING DEVICES IMPORT (splynx_monitoring → network_devices)
-- ============================================================================
-- Import Splynx monitoring devices into the NetworkDevice table.
-- Skip devices whose IP already exists (unique constraint on mgmt_ip).

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
    map.network_device_id,
    substring(COALESCE(NULLIF(m.title, ''), m.ip) from 1 for 160),
    substring(COALESCE(NULLIF(m.title, ''), m.ip) from 1 for 160),
    m.ip,
    -- Map Splynx integer type to DeviceType enum
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
    true,    -- ping_enabled
    false,   -- snmp_enabled
    'Imported from Splynx monitoring #' || m.id::text,
    m.id,
    NOT COALESCE(m.deleted, false),
    0,
    'unknown',
    NOW(),
    NOW()
FROM splynx_staging.splynx_monitoring m
JOIN splynx_staging.map_monitoring map ON map.splynx_monitoring_id = m.id
-- Skip if IP already exists in network_devices
WHERE m.ip IS NOT NULL AND btrim(m.ip) <> ''
    AND NOT EXISTS (
        SELECT 1 FROM network_devices nd WHERE nd.mgmt_ip = m.ip
    )
ON CONFLICT (id) DO NOTHING;

-- Cross-link NAS devices to monitoring devices by IP match
UPDATE nas_devices nd
SET network_device_id = ndev.id
FROM network_devices ndev
WHERE ndev.mgmt_ip = nd.management_ip
    AND ndev.mgmt_ip IS NOT NULL
    AND nd.network_device_id IS NULL;

-- ============================================================================
-- 22. SEED DOCUMENT SEQUENCES (ensure numbering continues after imports)
-- ============================================================================
-- Set next_value for each sequence to one past the last imported number.
-- Uses GREATEST() in ON CONFLICT to never go backward.

-- Subscriber number sequence (SUB-NNNNNN)
INSERT INTO document_sequences (id, key, next_value, created_at, updated_at)
SELECT
    gen_random_uuid(),
    'subscriber_number',
    COALESCE(max(substring(subscriber_number from 5)::int), 0) + 1,
    NOW(),
    NOW()
FROM subscribers
WHERE subscriber_number LIKE 'SUB-%'
ON CONFLICT ON CONSTRAINT uq_document_sequences_key
DO UPDATE SET
    next_value = GREATEST(document_sequences.next_value,
        (SELECT COALESCE(max(substring(subscriber_number from 5)::int), 0) + 1
         FROM subscribers WHERE subscriber_number LIKE 'SUB-%')),
    updated_at = NOW();

-- Account number sequence (ACC-NNNNNN)
INSERT INTO document_sequences (id, key, next_value, created_at, updated_at)
SELECT
    gen_random_uuid(),
    'account_number',
    COALESCE(max(substring(account_number from 5)::int), 0) + 1,
    NOW(),
    NOW()
FROM subscribers
WHERE account_number LIKE 'ACC-%'
ON CONFLICT ON CONSTRAINT uq_document_sequences_key
DO UPDATE SET
    next_value = GREATEST(document_sequences.next_value,
        (SELECT COALESCE(max(substring(account_number from 5)::int), 0) + 1
         FROM subscribers WHERE account_number LIKE 'ACC-%')),
    updated_at = NOW();

-- Invoice number sequence (INV-NNNNNN)
INSERT INTO document_sequences (id, key, next_value, created_at, updated_at)
SELECT
    gen_random_uuid(),
    'invoice_number',
    COALESCE(max(substring(invoice_number from 5)::int), 0) + 1,
    NOW(),
    NOW()
FROM invoices
WHERE invoice_number ~ '^INV-\d{6}$'
ON CONFLICT ON CONSTRAINT uq_document_sequences_key
DO UPDATE SET
    next_value = GREATEST(document_sequences.next_value,
        (SELECT COALESCE(max(substring(invoice_number from 5)::int), 0) + 1
         FROM invoices WHERE invoice_number ~ '^INV-\d{6}$')),
    updated_at = NOW();

-- ============================================================================
-- 23. MONITORING LOG → device_metrics (~2.1M rows)
-- ============================================================================
-- Import ping/SNMP status events from splynx_monitoring_log into device_metrics.
-- Each log entry is a status check: type=ping|snmp, status=ok|error.
-- Stored as custom metric: value=1 (ok) or value=0 (error), unit=type_status.

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
JOIN splynx_staging.map_monitoring map ON map.splynx_monitoring_id = ml.monitoring_id
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 24. TICKETS → splynx_archived_tickets (~14k rows)
-- ============================================================================

CREATE TABLE IF NOT EXISTS splynx_staging.map_tickets (
    splynx_ticket_id integer PRIMARY KEY,
    archived_ticket_id uuid NOT NULL
);

INSERT INTO splynx_staging.map_tickets (splynx_ticket_id, archived_ticket_id)
SELECT id, gen_random_uuid()
FROM splynx_staging.splynx_tickets
ON CONFLICT DO NOTHING;

INSERT INTO splynx_archived_tickets (
    id,
    splynx_ticket_id,
    subscriber_id,
    subject,
    status,
    priority,
    assigned_to,
    created_by,
    body,
    splynx_metadata,
    is_active,
    created_at,
    updated_at
)
SELECT
    tmap.archived_ticket_id,
    t.id,
    cmap.subscriber_id,
    substring(COALESCE(NULLIF(t.subject, ''), 'No subject') from 1 for 255),
    CASE t.status_id
        WHEN 1 THEN 'open'
        WHEN 2 THEN 'in_progress'
        WHEN 3 THEN 'waiting'
        WHEN 4 THEN 'resolved'
        WHEN 5 THEN 'closed'
        ELSE 'open'
    END,
    COALESCE(t.priority, 'normal'),
    -- Resolve assigned admin name
    (SELECT s.display_name FROM splynx_staging.map_admins ma
     JOIN subscribers s ON s.id = ma.subscriber_id
     WHERE ma.splynx_admin_id = t.assign_to
     LIMIT 1),
    -- Resolve reporter name
    CASE
        WHEN t.reporter_type = 'admin' THEN
            (SELECT s.display_name FROM splynx_staging.map_admins ma
             JOIN subscribers s ON s.id = ma.subscriber_id
             WHERE ma.splynx_admin_id = t.reporter_id
             LIMIT 1)
        ELSE
            (SELECT s.display_name FROM splynx_staging.map_customers mc
             JOIN subscribers s ON s.id = mc.subscriber_id
             WHERE mc.splynx_customer_id = t.reporter_id
             LIMIT 1)
    END,
    t.note,
    json_build_object(
        'splynx_group', t.group_id,
        'splynx_type_id', t.type_id,
        'splynx_source', t.source,
        'source', 'splynx_migration'
    )::jsonb,
    NOT COALESCE(t.deleted, false),
    COALESCE(t.created_at::timestamp with time zone, NOW()),
    COALESCE(t.updated_at::timestamp with time zone, NOW())
FROM splynx_staging.splynx_tickets t
JOIN splynx_staging.map_tickets tmap ON tmap.splynx_ticket_id = t.id
LEFT JOIN splynx_staging.map_customers cmap ON cmap.splynx_customer_id = t.customer_id
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 25. TICKET MESSAGES → splynx_archived_ticket_messages (~66k rows)
-- ============================================================================

INSERT INTO splynx_archived_ticket_messages (
    id,
    splynx_message_id,
    ticket_id,
    sender_type,
    sender_name,
    body,
    is_internal,
    created_at
)
SELECT
    gen_random_uuid(),
    tm.id,
    tmap.archived_ticket_id,
    COALESCE(tm.author_type, 'customer'),
    -- Resolve sender name
    CASE
        WHEN tm.admin_id IS NOT NULL AND tm.admin_id > 0 THEN
            (SELECT s.display_name FROM splynx_staging.map_admins ma
             JOIN subscribers s ON s.id = ma.subscriber_id
             WHERE ma.splynx_admin_id = tm.admin_id
             LIMIT 1)
        ELSE
            (SELECT s.display_name FROM splynx_staging.map_customers mc
             JOIN subscribers s ON s.id = mc.subscriber_id
             WHERE mc.splynx_customer_id = tm.customer_id
             LIMIT 1)
    END,
    -- message is bytea — convert to text safely
    convert_from(tm.message, 'UTF8'),
    COALESCE(tm.message_type = 'note', false),
    COALESCE(
        (tm.message_date::text || ' ' || COALESCE(tm.message_time::text, '00:00:00'))::timestamp with time zone,
        tm.updated_at::timestamp with time zone,
        NOW()
    )
FROM splynx_staging.splynx_ticket_messages tm
JOIN splynx_staging.map_tickets tmap ON tmap.splynx_ticket_id = tm.ticket_id
WHERE NOT COALESCE(tm.deleted, false)
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 26. QUOTES → splynx_archived_quotes (~6k rows)
-- ============================================================================

CREATE TABLE IF NOT EXISTS splynx_staging.map_quotes (
    splynx_quote_id integer PRIMARY KEY,
    archived_quote_id uuid NOT NULL
);

INSERT INTO splynx_staging.map_quotes (splynx_quote_id, archived_quote_id)
SELECT id, gen_random_uuid()
FROM splynx_staging.splynx_quotes
ON CONFLICT DO NOTHING;

INSERT INTO splynx_archived_quotes (
    id,
    splynx_quote_id,
    subscriber_id,
    quote_number,
    status,
    currency,
    subtotal,
    tax_total,
    total,
    valid_until,
    memo,
    splynx_metadata,
    is_active,
    created_at,
    updated_at
)
SELECT
    qmap.archived_quote_id,
    q.id,
    cmap.subscriber_id,
    q.number,
    COALESCE(q.status, 'draft'),
    'NGN',
    COALESCE(q.total, 0),   -- subtotal (no separate field in Splynx)
    0,                        -- tax_total (included in total)
    COALESCE(q.total, 0),
    q.date_till::timestamp with time zone,
    q.memo,
    json_build_object(
        'splynx_added_by', q.added_by,
        'splynx_added_by_id', q.added_by_id,
        'source', 'splynx_migration'
    )::jsonb,
    NOT COALESCE(q.deleted, false),
    COALESCE(q.real_create_datetime::timestamp with time zone, q.date_created::timestamp with time zone, NOW()),
    COALESCE(q.date_updated::timestamp with time zone, NOW())
FROM splynx_staging.splynx_quotes q
JOIN splynx_staging.map_quotes qmap ON qmap.splynx_quote_id = q.id
LEFT JOIN splynx_staging.map_customers cmap ON cmap.splynx_customer_id = q.customer_id
ON CONFLICT DO NOTHING;

-- ============================================================================
-- 27. QUOTE ITEMS → splynx_archived_quote_items (~12k rows)
-- ============================================================================

INSERT INTO splynx_archived_quote_items (
    id,
    splynx_item_id,
    quote_id,
    description,
    quantity,
    unit_price,
    amount,
    created_at
)
SELECT
    gen_random_uuid(),
    qi.id,
    qmap.archived_quote_id,
    qi.description,
    COALESCE(qi.quantity, 1),
    COALESCE(qi.price, 0),
    COALESCE(qi.quantity * qi.price, 0),
    NOW()
FROM splynx_staging.splynx_quotes_items qi
JOIN splynx_staging.map_quotes qmap ON qmap.splynx_quote_id = qi.quote_id
ON CONFLICT DO NOTHING;

COMMIT;
