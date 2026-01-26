-- Backfill Splynx fields into extended columns after migration.

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

-- Subscriber accounts from splynx customer billing
UPDATE subscriber_accounts a
SET
    billing_enabled = COALESCE(b.enabled, true),
    billing_person = b.billing_person,
    billing_street_1 = b.billing_street_1,
    billing_zip_code = b.billing_zip_code,
    billing_city = b.billing_city,
    deposit = b.deposit,
    payment_method = b.payment_method,
    billing_date = b.billing_date,
    billing_due = b.billing_due,
    grace_period = b.grace_period,
    min_balance = b.min_balance,
    month_price = b.month_price
FROM splynx_staging.map_customers map
JOIN splynx_staging.splynx_customer_billing b ON b.customer_id = map.splynx_customer_id
WHERE a.id = map.account_id;

-- People created/updated dates from splynx customers
UPDATE people p
SET
    created_at = COALESCE(c.date_add::timestamp, p.created_at),
    updated_at = COALESCE(c.last_update, p.updated_at)
FROM splynx_staging.map_customers map
JOIN splynx_staging.splynx_customers c ON c.id = map.splynx_customer_id
WHERE p.id = map.person_id;

COMMIT;
