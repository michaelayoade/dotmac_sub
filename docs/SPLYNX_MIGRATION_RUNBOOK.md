# Splynx Migration Runbook

This runbook uses the phased Python migration under `scripts/migration/` as the authoritative cutover path.

The legacy SQL bulk import in `scripts/splynx_migrate.sql` should be treated as reference/reconciliation only.

## Preconditions

- Docker services are running:
  - `dotmac_sub_db`
  - `dotmac_sub_app`
  - `dotmac_sub_celery_worker`
  - `dotmac_sub_celery_beat`
  - `dotmac_sub_radius_db`
  - `dotmac_sub_freeradius`
- `.env` contains:
  - `DATABASE_URL`
  - `CREDENTIAL_ENCRYPTION_KEY`
  - `SPLYNX_MYSQL_PASS`
  - `RADIUS_DB_DSN`
- Before running `radius_sync.py`, also set:
  - `SPLYNX_API_BASE`
  - `SPLYNX_API_KEY`
  - `SPLYNX_API_SECRET`
  - `SPLYNX_HOST_HEADER`
  - optionally `SPLYNX_VERIFY_TLS=false` if you intentionally need to bypass TLS verification

## One-Time Setup

```bash
cd /root/projects/dotmac_sub
source .env
make migrate
```

If you need API-backed password sync later:

```bash
export SPLYNX_API_BASE='https://your-splynx-host'
export SPLYNX_API_KEY='...'
export SPLYNX_API_SECRET='...'
export SPLYNX_HOST_HEADER='selfcare.example.com'
# export SPLYNX_VERIFY_TLS=false
```

## Dry-Run Rehearsal

```bash
cd /root/projects/dotmac_sub
source .env
poetry run python -m scripts.migration.phase0_reference_data
poetry run python -m scripts.migration.phase1_customers_services
poetry run python -m scripts.migration.phase2_financial_data
poetry run python -m scripts.migration.phase3_operational_data
poetry run python -m scripts.migration.incremental_sync --hours=24
```

## Clean Rehearsal Reset

Review first:

```bash
cd /root/projects/dotmac_sub
source .env
poetry run python -m scripts.migration.reset_for_clean_migration
```

Execute reset:

```bash
cd /root/projects/dotmac_sub
source .env
poetry run python -m scripts.migration.reset_for_clean_migration --execute
```

## Full Rehearsal / Cutover Import

Run in order:

```bash
cd /root/projects/dotmac_sub
source .env
poetry run python -m scripts.migration.phase0_reference_data --execute
poetry run python -m scripts.migration.phase1_customers_services --execute
poetry run python -m scripts.migration.phase2_financial_data --execute
poetry run python -m scripts.migration.phase3_operational_data --execute
```

Then run delta sync shortly before cutover:

```bash
cd /root/projects/dotmac_sub
source .env
poetry run python -m scripts.migration.incremental_sync --hours=48 --execute
```

## Automated Incremental Sync (Dual-Run)

During dual-run, enable automated incremental sync via Celery beat.
This pulls new invoices, payments, and status changes every 30 minutes.

Enable via environment variable:

```bash
export SPLYNX_SYNC_ENABLED=true
export SPLYNX_SYNC_INTERVAL_MINUTES=30  # default; min 5
```

Or via database setting:

```sql
INSERT INTO domain_settings (id, domain, key, value_text, value_type, is_active)
VALUES (gen_random_uuid(), 'subscriber', 'splynx_sync_enabled', 'true', 'text', true)
ON CONFLICT ON CONSTRAINT uq_domain_settings_domain_key DO UPDATE SET value_text = 'true';
```

Disable when ready to cut over (Splynx becomes read-only):

```bash
export SPLYNX_SYNC_ENABLED=false
```

## Metadata Backfill (Optional)

Enriches subscriber records with GPS, labels, billing email, and other
fields available only via the Splynx API. Requires API credentials.

```bash
cd /root/projects/dotmac_sub
source .env
export SPLYNX_API_BASE='https://your-splynx-host'
export SPLYNX_API_KEY='...'
export SPLYNX_API_SECRET='...'
export SPLYNX_HOST_HEADER='selfcare.example.com'
# Dry run (tests first 3 subscribers)
poetry run python -m scripts.migration.backfill_metadata
# Execute (rate-limited to 5 req/s, ~27 min for 8K subscribers)
poetry run python -m scripts.migration.backfill_metadata --execute
```

## RADIUS Sync

If you need Splynx cleartext passwords and FreeRADIUS population:

```bash
cd /root/projects/dotmac_sub
source .env
export SPLYNX_API_BASE='https://your-splynx-host'
export SPLYNX_API_KEY='...'
export SPLYNX_API_SECRET='...'
export SPLYNX_HOST_HEADER='selfcare.example.com'
poetry run python -m scripts.migration.radius_sync
poetry run python -m scripts.migration.radius_sync --execute
```

## Go/No-Go Checks

Run these after each rehearsal and before production cutover.

### Core Counts

```bash
docker exec -i dotmac_sub_db psql -U postgres -d dotmac_sub -c "
select 'subscribers' as metric, count(*) from subscribers
union all
select 'subscriptions', count(*) from subscriptions
union all
select 'invoices', count(*) from invoices
union all
select 'invoice_lines', count(*) from invoice_lines
union all
select 'payments', count(*) from payments
union all
select 'payment_allocations', count(*) from payment_allocations
union all
select 'credit_notes', count(*) from credit_notes
union all
select 'credit_note_applications', count(*) from credit_note_applications
union all
select 'mrr_snapshots', count(*) from mrr_snapshots
union all
select 'splynx_id_mappings', count(*) from splynx_id_mappings;
"
```

### Subscriber and Service Status Mix

```bash
docker exec -i dotmac_sub_db psql -U postgres -d dotmac_sub -c "
select status, count(*) from subscribers group by status order by status;
select status, count(*) from subscriptions group by status order by status;
select billing_mode, count(*) from subscriptions group by billing_mode order by billing_mode;
"
```

### Financial Totals

```bash
docker exec -i dotmac_sub_db psql -U postgres -d dotmac_sub -c "
select 'invoice_total' as metric, coalesce(sum(total),0) from invoices
union all
select 'invoice_balance_due', coalesce(sum(balance_due),0) from invoices
union all
select 'payments_total', coalesce(sum(amount),0) from payments
union all
select 'credit_notes_total', coalesce(sum(total),0) from credit_notes
union all
select 'ledger_total', coalesce(sum(amount),0) from ledger_entries;
"
```

### Duplicate Safety Checks

```bash
docker exec -i dotmac_sub_db psql -U postgres -d dotmac_sub -c "
select username, count(*) from access_credentials group by username having count(*) > 1;
select splynx_invoice_id, count(*) from invoices where splynx_invoice_id is not null group by splynx_invoice_id having count(*) > 1;
select splynx_payment_id, count(*) from payments where splynx_payment_id is not null group by splynx_payment_id having count(*) > 1;
"
```

### Auth Readiness

```bash
docker exec -i dotmac_sub_db psql -U postgres -d dotmac_sub -c "
select
  count(*) filter (where is_active) as active_credentials,
  count(*) filter (where is_active and secret_hash is not null) as active_with_secret
from access_credentials;
"
```

### Mapping Coverage

```bash
docker exec -i dotmac_sub_db psql -U postgres -d dotmac_sub -c "
select entity_type, count(*) from splynx_id_mappings group by entity_type order by entity_type;
"
```

## Recommended Production Sequence

1. Freeze non-essential config changes in DotMac Sub.
2. Take DotMac DB backup.
3. Run reset only if this is a clean replacement cutover.
4. Run phases 0-3 in order.
5. Run incremental sync for the final delta window.
6. Run reconciliation queries.
7. Run `radius_sync.py --execute` if PPPoE/RADIUS auth is part of cutover.
8. Switch operational traffic to DotMac Sub.
9. Watch app logs, Celery logs, and RADIUS auth results.

## Notes

- The phased Python migration is rerun-safe at record level for the corrected areas.
- `scripts/migration/backfill_metadata.py` uses env-var-based API credentials (same pattern as `radius_sync.py`). Rate-limited to 5 req/s.
- `scripts/splynx_staging.sql` is safe to keep for snapshotting, but it is not the primary import path.
- Incremental sync can run automatically via Celery beat (`SPLYNX_SYNC_ENABLED=true`) or manually via CLI.
