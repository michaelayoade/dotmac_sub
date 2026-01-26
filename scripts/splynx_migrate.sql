-- One-time migration from splynx_staging into dotmac_sm core tables.
-- Requires splynx_staging tables to be populated (see scripts/splynx_staging.sql).

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Mapping tables (Splynx ID -> local UUIDs)
CREATE TABLE IF NOT EXISTS splynx_staging.map_customers (
    splynx_customer_id integer PRIMARY KEY,
    person_id uuid NOT NULL,
    subscriber_id uuid NOT NULL,
    account_id uuid NOT NULL
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

CREATE TABLE IF NOT EXISTS splynx_staging.map_ipv4 (
    address text PRIMARY KEY,
    ipv4_id uuid NOT NULL
);

CREATE TABLE IF NOT EXISTS splynx_staging.map_ipv6 (
    address text PRIMARY KEY,
    ipv6_id uuid NOT NULL
);

CREATE TABLE IF NOT EXISTS splynx_staging.map_tickets (
    splynx_ticket_id integer PRIMARY KEY,
    ticket_id uuid NOT NULL
);

-- Seed mappings
INSERT INTO splynx_staging.map_customers (splynx_customer_id, person_id, subscriber_id, account_id)
SELECT id, gen_random_uuid(), gen_random_uuid(), gen_random_uuid()
FROM splynx_staging.splynx_customers
ON CONFLICT DO NOTHING;

INSERT INTO splynx_staging.map_tariffs (splynx_tariff_id, offer_id, offer_price_id)
SELECT id, gen_random_uuid(), gen_random_uuid()
FROM splynx_staging.splynx_tariffs_internet
ON CONFLICT DO NOTHING;

INSERT INTO splynx_staging.map_services (splynx_service_id, subscription_id, cpe_id)
SELECT id, gen_random_uuid(), gen_random_uuid()
FROM splynx_staging.splynx_services_internet
ON CONFLICT DO NOTHING;

INSERT INTO splynx_staging.map_invoices (splynx_invoice_id, invoice_id)
SELECT id, gen_random_uuid()
FROM splynx_staging.splynx_invoices
ON CONFLICT DO NOTHING;

INSERT INTO splynx_staging.map_tickets (splynx_ticket_id, ticket_id)
SELECT id, gen_random_uuid()
FROM splynx_staging.splynx_tickets
ON CONFLICT DO NOTHING;

-- People
WITH customer_data AS (
    SELECT
        c.*,
        map.person_id,
        map.subscriber_id,
        map.account_id,
        CASE
            WHEN c.email IS NULL OR btrim(c.email) = '' THEN NULL
            ELSE lower(btrim(c.email))
        END AS email_base
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
    SELECT email AS existing_email FROM people
)
INSERT INTO people (
    id,
    first_name,
    last_name,
    display_name,
    email,
    email_verified,
    phone,
    gender,
    status,
    is_active,
    marketing_opt_in,
    address_line1,
    city,
    postal_code,
    notes,
    created_at,
    updated_at
)
SELECT
    person_id,
    substring(COALESCE(NULLIF(split_part(name, ' ', 1), ''), 'Unknown') from 1 for 80),
    substring(COALESCE(NULLIF(trim(regexp_replace(name, '^[^ ]+\\s*', '')), ''), 'Customer') from 1 for 80),
    substring(name from 1 for 120),
    substring(
        CASE
            WHEN email_base IS NULL THEN 'splynx_customer_' || id::text || '@invalid.local'
            WHEN email_base IN (SELECT existing_email FROM existing_email) THEN regexp_replace(email_base, '@', '+splynx' || id::text || '@')
            WHEN email_seq = 1 THEN email_base
            ELSE regexp_replace(email_base, '@', '+' || email_seq::text || '@')
        END
        from 1 for 255
    ),
    false,
    substring(phone from 1 for 40),
    'unknown'::gender,
    CASE WHEN COALESCE(deleted, false) THEN 'inactive'::personstatus ELSE 'active'::personstatus END,
    NOT COALESCE(deleted, false),
    false,
    substring(street_1 from 1 for 120),
    city,
    substring(zip_code from 1 for 20),
    NULL,
    COALESCE(date_add::timestamp, NOW()),
    COALESCE(last_update, NOW())
FROM email_dedup
ON CONFLICT (id) DO NOTHING;

-- Subscribers
INSERT INTO subscribers (
    id,
    subscriber_type,
    subscriber_number,
    person_id,
    organization_id,
    is_active,
    notes,
    created_at,
    updated_at
)
SELECT
    map.subscriber_id,
    'person'::subscribertype,
    'splynx_' || c.id::text,
    map.person_id,
    NULL,
    NOT COALESCE(c.deleted, false),
    NULL,
    NOW(),
    NOW()
FROM splynx_staging.splynx_customers c
JOIN splynx_staging.map_customers map ON map.splynx_customer_id = c.id
ON CONFLICT (id) DO NOTHING;

-- Subscriber accounts
INSERT INTO subscriber_accounts (
    id,
    subscriber_id,
    account_number,
    status,
    billing_enabled,
    billing_person,
    billing_street_1,
    billing_zip_code,
    billing_city,
    deposit,
    payment_method,
    billing_date,
    billing_due,
    grace_period,
    min_balance,
    month_price,
    notes,
    created_at,
    updated_at
)
SELECT
    map.account_id,
    map.subscriber_id,
    COALESCE(NULLIF(c.login, ''), c.id::text),
    CASE
        WHEN COALESCE(c.deleted, false) THEN 'canceled'
        WHEN c.status IN ('disabled', 'blocked') THEN 'suspended'
        ELSE 'active'
    END::accountstatus,
    COALESCE(b.enabled, true),
    b.billing_person,
    b.billing_street_1,
    b.billing_zip_code,
    b.billing_city,
    b.deposit,
    b.payment_method,
    b.billing_date,
    b.billing_due,
    b.grace_period,
    b.min_balance,
    b.month_price,
    c.billing_type,
    NOW(),
    NOW()
FROM splynx_staging.splynx_customers c
JOIN splynx_staging.map_customers map ON map.splynx_customer_id = c.id
LEFT JOIN splynx_staging.splynx_customer_billing b ON b.customer_id = c.id
ON CONFLICT (id) DO NOTHING;

-- Service addresses (from customer)
INSERT INTO addresses (
    id,
    subscriber_id,
    account_id,
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
    map.account_id,
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
WHERE c.street_1 IS NOT NULL OR c.city IS NOT NULL OR c.zip_code IS NOT NULL;

-- Billing addresses (from customer_billing)
INSERT INTO addresses (
    id,
    subscriber_id,
    account_id,
    address_type,
    address_line1,
    city,
    postal_code,
    is_primary,
    created_at,
    updated_at
)
SELECT
    gen_random_uuid(),
    map.subscriber_id,
    map.account_id,
    'billing'::addresstype,
    substring(b.billing_street_1 from 1 for 120),
    b.billing_city,
    substring(b.billing_zip_code from 1 for 20),
    false,
    NOW(),
    NOW()
FROM splynx_staging.splynx_customer_billing b
JOIN splynx_staging.map_customers map ON map.splynx_customer_id = b.customer_id
WHERE b.billing_street_1 IS NOT NULL OR b.billing_city IS NOT NULL OR b.billing_zip_code IS NOT NULL;

-- Catalog offers, prices
INSERT INTO catalog_offers (
    id,
    name,
    code,
    service_type,
    access_type,
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

-- Subscriptions + CPE devices
INSERT INTO subscriptions (
    id,
    account_id,
    offer_id,
    offer_version_id,
    service_address_id,
    status,
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
    created_at,
    updated_at
)
SELECT
    map.subscription_id,
    cust_map.account_id,
    tariff_map.offer_id,
    NULL,
    NULL,
    CASE
        WHEN s.status = 'active' THEN 'active'
        WHEN s.status IN ('disabled', 'hidden') THEN 'suspended'
        WHEN s.status = 'stopped' THEN 'canceled'
        ELSE 'pending'
    END::subscriptionstatus,
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
    NOW(),
    NOW()
FROM splynx_staging.splynx_services_internet s
JOIN splynx_staging.map_services map ON map.splynx_service_id = s.id
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = s.customer_id
JOIN splynx_staging.map_tariffs tariff_map ON tariff_map.splynx_tariff_id = s.tariff_id
ON CONFLICT (id) DO NOTHING;

INSERT INTO cpe_devices (
    id,
    account_id,
    subscription_id,
    device_type,
    status,
    serial_number,
    model,
    vendor,
    mac_address,
    installed_at,
    notes,
    created_at,
    updated_at
)
SELECT
    map.cpe_id,
    cust_map.account_id,
    map.subscription_id,
    'ont'::devicetype,
    'active'::cpe_devicestatus,
    NULL,
    NULL,
    NULL,
    s.mac,
    s.start_date,
    s.login,
    NOW(),
    NOW()
FROM splynx_staging.splynx_services_internet s
JOIN splynx_staging.map_services map ON map.splynx_service_id = s.id
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = s.customer_id
ON CONFLICT (id) DO NOTHING;

-- IP address pools
INSERT INTO splynx_staging.map_ipv4 (address, ipv4_id)
SELECT DISTINCT s.ipv4, gen_random_uuid()
FROM splynx_staging.splynx_services_internet s
WHERE s.ipv4 IS NOT NULL AND s.ipv4 <> ''
ON CONFLICT DO NOTHING;

INSERT INTO splynx_staging.map_ipv6 (address, ipv6_id)
SELECT DISTINCT s.ipv6, gen_random_uuid()
FROM splynx_staging.splynx_services_internet s
WHERE s.ipv6 IS NOT NULL AND s.ipv6 <> ''
ON CONFLICT DO NOTHING;

INSERT INTO ipv4_addresses (id, address, is_reserved, created_at, updated_at)
SELECT map.ipv4_id, map.address, false, NOW(), NOW()
FROM splynx_staging.map_ipv4 map
ON CONFLICT (id) DO NOTHING;

INSERT INTO ipv6_addresses (id, address, is_reserved, created_at, updated_at)
SELECT map.ipv6_id, map.address, false, NOW(), NOW()
FROM splynx_staging.map_ipv6 map
ON CONFLICT (id) DO NOTHING;

INSERT INTO ip_assignments (
    id,
    account_id,
    subscription_id,
    ip_version,
    ipv4_address_id,
    ipv6_address_id,
    is_active,
    created_at,
    updated_at
)
SELECT DISTINCT ON (v4.ipv4_id)
    gen_random_uuid(),
    cust_map.account_id,
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
WHERE s.ipv4 IS NOT NULL AND s.ipv4 <> ''
ORDER BY v4.ipv4_id, s.id;

INSERT INTO ip_assignments (
    id,
    account_id,
    subscription_id,
    ip_version,
    ipv4_address_id,
    ipv6_address_id,
    is_active,
    created_at,
    updated_at
)
SELECT DISTINCT ON (v6.ipv6_id)
    gen_random_uuid(),
    cust_map.account_id,
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
WHERE (s.ipv4 IS NULL OR s.ipv4 = '') AND s.ipv6 IS NOT NULL AND s.ipv6 <> ''
ORDER BY v6.ipv6_id, s.id;

-- Invoices + lines
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
    issued_at,
    due_at,
    paid_at,
    memo,
    is_active,
    created_at,
    updated_at
)
SELECT
    map.invoice_id,
    cust_map.account_id,
    i.number,
    CASE
        WHEN i.status = 'paid' THEN 'paid'
        WHEN i.status = 'not_paid' THEN 'issued'
        ELSE 'void'
    END::invoicestatus,
    'NGN',
    COALESCE(i.total, 0),
    0,
    COALESCE(i.total, 0),
    COALESCE(i.due, 0),
    COALESCE(i.real_create_datetime, i.date_created),
    i.date_till,
    i.date_payment,
    COALESCE(i.memo, i.note),
    NOT COALESCE(i.deleted, false),
    NOW(),
    NOW()
FROM splynx_staging.splynx_invoices i
JOIN splynx_staging.map_invoices map ON map.splynx_invoice_id = i.id
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = i.customer_id
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
    li.description,
    COALESCE(li.quantity, 1),
    COALESCE(li.unit_price, 0),
    COALESCE(li.total, 0),
    'exclusive'::taxapplication,
    true,
    NOW(),
    NOW()
FROM splynx_staging.splynx_invoice_items li
JOIN splynx_staging.map_invoices inv_map ON inv_map.splynx_invoice_id = li.invoice_id;

-- Payments
INSERT INTO payments (
    id,
    account_id,
    invoice_id,
    amount,
    currency,
    status,
    paid_at,
    external_id,
    memo,
    is_active,
    created_at,
    updated_at
)
SELECT
    gen_random_uuid(),
    cust_map.account_id,
    inv_map.invoice_id,
    COALESCE(p.amount, 0),
    'NGN',
    'succeeded'::paymentstatus,
    COALESCE(p.real_create_datetime, p.payment_date),
    COALESCE(p.transaction_id::text, p.receipt_number),
    COALESCE(p.memo, p.note, p.comment),
    NOT COALESCE(p.deleted, false),
    NOW(),
    NOW()
FROM splynx_staging.splynx_payments p
JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = p.customer_id
LEFT JOIN splynx_staging.map_invoices inv_map ON inv_map.splynx_invoice_id = p.invoice_id;

-- Tickets
INSERT INTO tickets (
    id,
    account_id,
    title,
    description,
    status,
    priority,
    channel,
    is_active,
    created_at,
    updated_at
)
SELECT
    map.ticket_id,
    cust_map.account_id,
    COALESCE(t.subject, 'Splynx Ticket #' || t.id::text),
    t.note,
    CASE WHEN COALESCE(t.closed, false) THEN 'closed' ELSE 'open' END::ticketstatus,
    CASE
        WHEN t.priority = 'medium' THEN 'normal'
        WHEN t.priority IN ('low', 'high', 'urgent') THEN t.priority
        ELSE 'normal'
    END::ticketpriority,
    CASE
        WHEN t.source ILIKE '%email%' THEN 'email'
        WHEN t.source ILIKE '%phone%' THEN 'phone'
        ELSE 'web'
    END::ticketchannel,
    NOT COALESCE(t.deleted, false),
    COALESCE(t.created_at, NOW()),
    COALESCE(t.updated_at, NOW())
FROM splynx_staging.splynx_tickets t
JOIN splynx_staging.map_tickets map ON map.splynx_ticket_id = t.id
LEFT JOIN splynx_staging.map_customers cust_map ON cust_map.splynx_customer_id = t.customer_id
ON CONFLICT (id) DO NOTHING;

-- Ticket comments (bytea -> text via escape encoding)
INSERT INTO ticket_comments (
    id,
    ticket_id,
    body,
    is_internal,
    created_at
)
SELECT
    gen_random_uuid(),
    ticket_map.ticket_id,
    encode(m.message, 'escape'),
    false,
    COALESCE(
        (m.message_date::text || ' ' || m.message_time::text)::timestamp,
        NOW()
    )
FROM splynx_staging.splynx_ticket_messages m
JOIN splynx_staging.map_tickets ticket_map ON ticket_map.splynx_ticket_id = m.ticket_id;

COMMIT;
