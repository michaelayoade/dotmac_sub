-- One-time staging setup for Splynx import.
-- Creates foreign tables to the remote Splynx DB, then snapshots them into local staging tables.

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
        splynx_customers,
        splynx_customer_billing,
        splynx_invoices,
        splynx_invoice_items,
        splynx_payments,
        splynx_services_internet,
        splynx_tariffs_internet,
        splynx_tickets,
        splynx_ticket_messages,
        splynx_partners,
        splynx_locations
    )
    FROM SERVER splynx_server INTO splynx_fdw;

CREATE SCHEMA IF NOT EXISTS splynx_staging;

-- Snapshot each table into local staging for a stable one-time import.
CREATE TABLE IF NOT EXISTS splynx_staging.splynx_customers
    (LIKE splynx_fdw.splynx_customers INCLUDING ALL);
TRUNCATE splynx_staging.splynx_customers;
INSERT INTO splynx_staging.splynx_customers SELECT * FROM splynx_fdw.splynx_customers;

CREATE TABLE IF NOT EXISTS splynx_staging.splynx_customer_billing
    (LIKE splynx_fdw.splynx_customer_billing INCLUDING ALL);
TRUNCATE splynx_staging.splynx_customer_billing;
INSERT INTO splynx_staging.splynx_customer_billing SELECT * FROM splynx_fdw.splynx_customer_billing;

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

CREATE TABLE IF NOT EXISTS splynx_staging.splynx_services_internet
    (LIKE splynx_fdw.splynx_services_internet INCLUDING ALL);
TRUNCATE splynx_staging.splynx_services_internet;
INSERT INTO splynx_staging.splynx_services_internet SELECT * FROM splynx_fdw.splynx_services_internet;

CREATE TABLE IF NOT EXISTS splynx_staging.splynx_tariffs_internet
    (LIKE splynx_fdw.splynx_tariffs_internet INCLUDING ALL);
TRUNCATE splynx_staging.splynx_tariffs_internet;
INSERT INTO splynx_staging.splynx_tariffs_internet SELECT * FROM splynx_fdw.splynx_tariffs_internet;

CREATE TABLE IF NOT EXISTS splynx_staging.splynx_tickets
    (LIKE splynx_fdw.splynx_tickets INCLUDING ALL);
TRUNCATE splynx_staging.splynx_tickets;
INSERT INTO splynx_staging.splynx_tickets SELECT * FROM splynx_fdw.splynx_tickets;

CREATE TABLE IF NOT EXISTS splynx_staging.splynx_ticket_messages
    (LIKE splynx_fdw.splynx_ticket_messages INCLUDING ALL);
TRUNCATE splynx_staging.splynx_ticket_messages;
INSERT INTO splynx_staging.splynx_ticket_messages SELECT * FROM splynx_fdw.splynx_ticket_messages;

CREATE TABLE IF NOT EXISTS splynx_staging.splynx_partners
    (LIKE splynx_fdw.splynx_partners INCLUDING ALL);
TRUNCATE splynx_staging.splynx_partners;
INSERT INTO splynx_staging.splynx_partners SELECT * FROM splynx_fdw.splynx_partners;

CREATE TABLE IF NOT EXISTS splynx_staging.splynx_locations
    (LIKE splynx_fdw.splynx_locations INCLUDING ALL);
TRUNCATE splynx_staging.splynx_locations;
INSERT INTO splynx_staging.splynx_locations SELECT * FROM splynx_fdw.splynx_locations;

COMMIT;
