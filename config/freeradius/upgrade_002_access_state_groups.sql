-- Phase 1 of the RADIUS access-state refactor.
-- See docs/radius_state_refactor/phase0_state_model.md
--
-- Provisions the three RADIUS groups the new state model maps to:
--   dotmac-active     — normal customers (informational reply attrs)
--   dotmac-suspended  — hard block (Auth-Type := Reject)
--   dotmac-captive    — soft block, routes to captive portal
--
-- This migration is purely additive:
--   * Nothing in the app reads radusergroup yet (phase 3 starts dual-write)
--   * Existing per-user radcheck/radreply rows still drive every auth
--   * The five reject CIDR pools and per-customer address-lists stay
--     operational as belt-and-suspenders through phase 8
--
-- Apply with:  psql -U radius -d radius -f upgrade_002_access_state_groups.sql
-- Rollback:    DELETE FROM radgroupcheck WHERE groupname LIKE 'dotmac-%';
--              DELETE FROM radgroupreply WHERE groupname LIKE 'dotmac-%';
--
-- Safe to re-run: wraps in a transaction, deletes the dotmac-* group rows
-- before re-inserting so the state is deterministic.

BEGIN;

-- Idempotency: wipe any prior dotmac-* group rows. Lets the script serve
-- as both the initial install and the canonical "redeploy" path when we
-- adjust group attrs later.
DELETE FROM radgroupcheck WHERE groupname IN (
    'dotmac-active', 'dotmac-suspended', 'dotmac-captive'
);
DELETE FROM radgroupreply WHERE groupname IN (
    'dotmac-active', 'dotmac-suspended', 'dotmac-captive'
);

-- dotmac-active
-- Informational only during phases 1-7. Per-user radreply rows still
-- carry the actual IP and rate-limit until phase 7 cutover.
INSERT INTO radgroupreply (groupname, attribute, op, value) VALUES
    ('dotmac-active', 'Service-Type',        ':=', 'Framed-User'),
    ('dotmac-active', 'Framed-Protocol',     ':=', 'PPP');

-- dotmac-suspended
-- Auth-Type := Reject in radgroupcheck rejects the auth before any reply
-- attrs are evaluated. No IP issued, no live session permitted.
INSERT INTO radgroupcheck (groupname, attribute, op, value) VALUES
    ('dotmac-suspended', 'Auth-Type', ':=', 'Reject');

-- dotmac-captive
-- Customer authenticates and gets an IP from the captive pool. The
-- captive pool CIDR has a standing dst-nat tcp/80 -> portal_ip rule at
-- each NAS so HTTP requests redirect to the payment portal. DNS and
-- portal traffic are allowed; everything else is dropped at the NAS
-- standing chain.
--
-- Framed-Pool refers to a Mikrotik /ip pool name; the operator
-- provisions the pool by hand per phase 1 runbook.
INSERT INTO radgroupreply (groupname, attribute, op, value) VALUES
    ('dotmac-captive', 'Service-Type',         ':=', 'Framed-User'),
    ('dotmac-captive', 'Framed-Protocol',      ':=', 'PPP'),
    ('dotmac-captive', 'Framed-Pool',          ':=', 'dotmac-captive-pool'),
    ('dotmac-captive', 'Mikrotik-Rate-Limit',  ':=', '1M/1M'),
    ('dotmac-captive', 'Mikrotik-Address-List',':=', 'dotmac-captive');

COMMIT;
