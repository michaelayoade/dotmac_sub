-- One-time staging setup for Splynx import.
-- Creates foreign tables to the remote Splynx DB, then snapshots them into local staging tables.
-- This captures a point-in-time snapshot for a stable one-time migration.

BEGIN;

CREATE EXTENSION IF NOT EXISTS postgres_fdw;

DROP SCHEMA IF EXISTS splynx_fdw CASCADE;
CREATE SCHEMA splynx_fdw;

DROP SERVER IF EXISTS splynx_server CASCADE;
CREATE SERVER splynx_server
    FOREIGN DATA WRAPPER postgres_fdw
    OPTIONS (host '149.102.135.97', port '5435', dbname 'splynx_db');

CREATE USER MAPPING FOR postgres
    SERVER splynx_server
    OPTIONS (user 'postgres', password 'BBglrgPlwQLHprB3+cgQPk7dKwbZYXKH');

IMPORT FOREIGN SCHEMA public
    LIMIT TO (
        -- Original tables
        splynx_customers,
        splynx_customer_billing,
        splynx_customer_info,
        splynx_invoices,
        splynx_invoice_items,
        splynx_payments,
        splynx_services_internet,
        splynx_tariffs_internet,
        splynx_routers,
        splynx_billing_transactions,
        splynx_partners,
        splynx_locations,
        -- New tables (added 2026-02-16)
        splynx_admins,
        splynx_accounting_customers,
        splynx_customers_values,
        splynx_inventory_items,
        splynx_ipv4_networks_ip,
        splynx_quotes,
        splynx_quotes_items,
        splynx_monitoring,
        splynx_monitoring_log,
        splynx_tickets,
        splynx_ticket_messages,
        splynx_mrr_statistics,
        -- Large tables (staged in batches by splynx_migrate_phase2.sql):
        splynx_statistics,
        splynx_traffic_counter
    )
    FROM SERVER splynx_server INTO splynx_fdw;

CREATE SCHEMA IF NOT EXISTS splynx_staging;

-- ============================================================================
-- Snapshot each table into local staging for a stable one-time import.
-- ============================================================================

-- Reference data
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_partners
    (LIKE splynx_fdw.splynx_partners INCLUDING ALL);
TRUNCATE splynx_staging.splynx_partners;
INSERT INTO splynx_staging.splynx_partners SELECT * FROM splynx_fdw.splynx_partners;

CREATE TABLE IF NOT EXISTS splynx_staging.splynx_locations
    (LIKE splynx_fdw.splynx_locations INCLUDING ALL);
TRUNCATE splynx_staging.splynx_locations;
INSERT INTO splynx_staging.splynx_locations SELECT * FROM splynx_fdw.splynx_locations;

-- Customers
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_customers
    (LIKE splynx_fdw.splynx_customers INCLUDING ALL);
TRUNCATE splynx_staging.splynx_customers;
INSERT INTO splynx_staging.splynx_customers SELECT * FROM splynx_fdw.splynx_customers;

CREATE TABLE IF NOT EXISTS splynx_staging.splynx_customer_billing
    (LIKE splynx_fdw.splynx_customer_billing INCLUDING ALL);
TRUNCATE splynx_staging.splynx_customer_billing;
INSERT INTO splynx_staging.splynx_customer_billing SELECT * FROM splynx_fdw.splynx_customer_billing;

CREATE TABLE IF NOT EXISTS splynx_staging.splynx_customer_info
    (LIKE splynx_fdw.splynx_customer_info INCLUDING ALL);
TRUNCATE splynx_staging.splynx_customer_info;
INSERT INTO splynx_staging.splynx_customer_info SELECT * FROM splynx_fdw.splynx_customer_info;

-- Catalog
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_tariffs_internet
    (LIKE splynx_fdw.splynx_tariffs_internet INCLUDING ALL);
TRUNCATE splynx_staging.splynx_tariffs_internet;
INSERT INTO splynx_staging.splynx_tariffs_internet SELECT * FROM splynx_fdw.splynx_tariffs_internet;

-- Services
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_services_internet
    (LIKE splynx_fdw.splynx_services_internet INCLUDING ALL);
TRUNCATE splynx_staging.splynx_services_internet;
INSERT INTO splynx_staging.splynx_services_internet SELECT * FROM splynx_fdw.splynx_services_internet;

-- Billing
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_invoices
    (LIKE splynx_fdw.splynx_invoices INCLUDING ALL);
TRUNCATE splynx_staging.splynx_invoices;
INSERT INTO splynx_staging.splynx_invoices SELECT * FROM splynx_fdw.splynx_invoices;

CREATE TABLE IF NOT EXISTS splynx_staging.splynx_invoice_items
    (LIKE splynx_fdw.splynx_invoice_items INCLUDING ALL);
TRUNCATE splynx_staging.splynx_invoice_items;
INSERT INTO splynx_staging.splynx_invoice_items SELECT * FROM splynx_fdw.splynx_invoice_items;

CREATE TABLE IF NOT EXISTS splynx_staging.splynx_payments
    (LIKE splynx_fdw.splynx_payments INCLUDING ALL);
TRUNCATE splynx_staging.splynx_payments;
INSERT INTO splynx_staging.splynx_payments SELECT * FROM splynx_fdw.splynx_payments;

CREATE TABLE IF NOT EXISTS splynx_staging.splynx_billing_transactions
    (LIKE splynx_fdw.splynx_billing_transactions INCLUDING ALL);
TRUNCATE splynx_staging.splynx_billing_transactions;
INSERT INTO splynx_staging.splynx_billing_transactions SELECT * FROM splynx_fdw.splynx_billing_transactions;

-- Network
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_routers
    (LIKE splynx_fdw.splynx_routers INCLUDING ALL);
TRUNCATE splynx_staging.splynx_routers;
INSERT INTO splynx_staging.splynx_routers SELECT * FROM splynx_fdw.splynx_routers;

-- ============================================================================
-- New tables (added 2026-02-16)
-- ============================================================================

-- Admins (staff users, ~469 rows)
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_admins
    (LIKE splynx_fdw.splynx_admins INCLUDING ALL);
TRUNCATE splynx_staging.splynx_admins;
INSERT INTO splynx_staging.splynx_admins SELECT * FROM splynx_fdw.splynx_admins;

-- Accounting customers (Zoho/external accounting links, ~15k rows)
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_accounting_customers
    (LIKE splynx_fdw.splynx_accounting_customers INCLUDING ALL);
TRUNCATE splynx_staging.splynx_accounting_customers;
INSERT INTO splynx_staging.splynx_accounting_customers SELECT * FROM splynx_fdw.splynx_accounting_customers;

-- Customer custom field values (zoho_id, social_id, ~26k rows)
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_customers_values
    (LIKE splynx_fdw.splynx_customers_values INCLUDING ALL);
TRUNCATE splynx_staging.splynx_customers_values;
INSERT INTO splynx_staging.splynx_customers_values SELECT * FROM splynx_fdw.splynx_customers_values;

-- Inventory items (hardware serial numbers, barcodes, ~17k rows)
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_inventory_items
    (LIKE splynx_fdw.splynx_inventory_items INCLUDING ALL);
TRUNCATE splynx_staging.splynx_inventory_items;
INSERT INTO splynx_staging.splynx_inventory_items SELECT * FROM splynx_fdw.splynx_inventory_items;

-- IPv4 network IPs (IP address management with pools, ~18k rows)
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_ipv4_networks_ip
    (LIKE splynx_fdw.splynx_ipv4_networks_ip INCLUDING ALL);
TRUNCATE splynx_staging.splynx_ipv4_networks_ip;
INSERT INTO splynx_staging.splynx_ipv4_networks_ip SELECT * FROM splynx_fdw.splynx_ipv4_networks_ip;

-- Quotes (~6k rows)
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_quotes
    (LIKE splynx_fdw.splynx_quotes INCLUDING ALL);
TRUNCATE splynx_staging.splynx_quotes;
INSERT INTO splynx_staging.splynx_quotes SELECT * FROM splynx_fdw.splynx_quotes;

-- Quote line items (~12k rows)
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_quotes_items
    (LIKE splynx_fdw.splynx_quotes_items INCLUDING ALL);
TRUNCATE splynx_staging.splynx_quotes_items;
INSERT INTO splynx_staging.splynx_quotes_items SELECT * FROM splynx_fdw.splynx_quotes_items;

-- Network monitoring devices (~607 rows)
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_monitoring
    (LIKE splynx_fdw.splynx_monitoring INCLUDING ALL);
TRUNCATE splynx_staging.splynx_monitoring;
INSERT INTO splynx_staging.splynx_monitoring SELECT * FROM splynx_fdw.splynx_monitoring;

-- Monitoring log (~2.1M rows) — captured for reference/analytics only, not imported
-- into core tables. Query this staging table directly for historical monitoring data.
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_monitoring_log
    (LIKE splynx_fdw.splynx_monitoring_log INCLUDING ALL);
TRUNCATE splynx_staging.splynx_monitoring_log;
INSERT INTO splynx_staging.splynx_monitoring_log SELECT * FROM splynx_fdw.splynx_monitoring_log;

-- Tickets (~14k rows)
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_tickets
    (LIKE splynx_fdw.splynx_tickets INCLUDING ALL);
TRUNCATE splynx_staging.splynx_tickets;
INSERT INTO splynx_staging.splynx_tickets SELECT * FROM splynx_fdw.splynx_tickets;

-- Ticket messages (~66k rows)
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_ticket_messages
    (LIKE splynx_fdw.splynx_ticket_messages INCLUDING ALL);
TRUNCATE splynx_staging.splynx_ticket_messages;
INSERT INTO splynx_staging.splynx_ticket_messages SELECT * FROM splynx_fdw.splynx_ticket_messages;

-- MRR statistics (~3.3M rows)
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_mrr_statistics
    (LIKE splynx_fdw.splynx_mrr_statistics INCLUDING ALL);
TRUNCATE splynx_staging.splynx_mrr_statistics;
INSERT INTO splynx_staging.splynx_mrr_statistics SELECT * FROM splynx_fdw.splynx_mrr_statistics;

-- ============================================================================
-- Large tables (FDW foreign tables created above; NOT snapshotted here)
-- These are staged in batches by splynx_migrate_phase2.sql to avoid
-- pulling millions of rows over the network in a single transaction.
-- ============================================================================

-- RADIUS sessions (millions of rows) — staged in batches by splynx_migrate_phase2.sql
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_statistics
    (LIKE splynx_fdw.splynx_statistics INCLUDING ALL);

-- Traffic counters (millions+ rows) — staged in batches by splynx_migrate_phase2.sql
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_traffic_counter
    (LIKE splynx_fdw.splynx_traffic_counter INCLUDING ALL);

COMMIT;
