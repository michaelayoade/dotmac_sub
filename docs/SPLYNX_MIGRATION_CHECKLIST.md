# Splynx → DotMac Sub Migration Checklist

Companion to [`SPLYNX_MIGRATION_RUNBOOK.md`](./SPLYNX_MIGRATION_RUNBOOK.md). The runbook has the
commands; this is the **tracked checklist** with the gaps and manual steps the runbook omits.

> All phase scripts are **dry-run by default** and require `--execute` to write.
> All are idempotent at record level (check `splynx_id_mappings` before insert) unless noted.

---

## 0. Pre-flight (do before touching data)

- [ ] Confirm scope: same operator/Splynx instance the tooling was built for, **or** a new ISP
      (new ISP ⇒ re-map the hardwired assumptions below before running anything)
- [ ] Docker services up: `dotmac_sub_db`, `dotmac_sub_app`, `dotmac_sub_celery_worker`,
      `dotmac_sub_celery_beat`, `dotmac_sub_radius_db`, `dotmac_sub_freeradius`
- [ ] `.env` present with `DATABASE_URL`, `CREDENTIAL_ENCRYPTION_KEY`, `SPLYNX_MYSQL_PASS`, `RADIUS_DB_DSN`
- [ ] Splynx API creds set (needed for passwords/metadata/RADIUS): `SPLYNX_API_BASE`, `SPLYNX_API_KEY`,
      `SPLYNX_API_SECRET`, `SPLYNX_HOST_HEADER`
- [ ] `make migrate` run (DotMac schema at head)
- [ ] **Full DotMac DB backup taken**
- [ ] Splynx read path reachable (MySQL + API) from the app host
- [ ] S3 bucket exists (`ensure_storage_bucket()`) if migrating blobs

### Hardwired assumptions to re-map if you are NOT the original operator
- [ ] Country `"NG"` / currency `"NGN"` (`phase1`, `phase0`)
- [ ] Ledger category IDs 1–22 → `LedgerCategory` map (`phase2_financial_data.py`)
- [ ] OLT board maps / config packs / traffic tables (`smartolt_sync.py`, `populate_olt_config_packs.py`)
- [ ] Splynx host/IP and SmartOLT account (`import_splynx_monitoring_devices.py`, `smartolt_sync.py`)

---

## 1. Dry-run rehearsal (no `--execute`)

- [ ] `phase0_reference_data` — counts only, no errors
- [ ] `phase1_customers_services`
- [ ] `phase2_financial_data`
- [ ] `phase3_operational_data`
- [ ] `incremental_sync --hours=24`
- [ ] Review `docs/migration/unmapped_subs_for_review.csv` (subs missing RADIUS login — manual triage)

---

## 2. Clean reset (only for a clean replacement cutover)

- [ ] Review preserved-vs-truncated list in `reset_for_clean_migration.py`
- [ ] `reset_for_clean_migration.py` (dry-run) reviewed
- [ ] `reset_for_clean_migration.py --execute`
      (preserves RBAC, domain_settings, sequences, real admin accounts; truncates the rest)

---

## 3. Full import (`--execute`, in order)

- [ ] Phase 0 — reference data
- [ ] Phase 1 — customers & services
- [ ] Phase 2 — financial data
- [ ] Phase 3 — operational data
- [ ] Phase 5 — `phase5_backfill_access_state.py` (access_state + RADIUS radusergroup)
- [ ] Portal logins — `bootstrap_portal_credentials.py --execute --active-only`
- [ ] (optional) Metadata enrich — `backfill_metadata.py --execute` (API, ~27 min/8k subs)

---

## 4. RADIUS / network provisioning

- [ ] `bootstrap_radius_from_splynx.py --execute` (cleartext PPPoE pw → radcheck/radreply) — **canary `--limit 100` first**
- [ ] `populate_radius_from_subs.py --execute` (DotMac DB becomes RADIUS source of truth)
- [ ] **MANUAL:** generate `/etc/freeradius/clients.conf` from the `nas` table
- [ ] **MANUAL:** repoint BNG/routers at the new FreeRADIUS server + secrets
- [ ] ONT/serial enrichment (needs SmartOLT API + SSH to OLTs):
      `smartolt_sync.py --execute`, `fix_hw_serials.py --execute`, `bulk_allocate_mgmt_ips.py --execute`
- [ ] `import_splynx_monitoring_devices.py` (monitoring inventory)

---

## 5. Blobs → S3

- [ ] `migrate_branding_to_s3.py`
- [ ] `migrate_invoice_pdf_exports_to_s3.py`
- [ ] `migrate_legal_files_to_s3.py`

---

## 6. Dual-run (both systems live)

- [ ] Enable `SPLYNX_SYNC_ENABLED=true` (interval `SPLYNX_SYNC_INTERVAL_MINUTES`, default 30)
- [ ] Confirm Celery beat picking up `run_incremental_sync`
- [ ] Monitor new invoices/payments/status flowing into DotMac

---

## 7. Reconciliation (Go/No-Go)

Run the runbook SQL blocks **and**:

- [ ] Core counts match Splynx (subscribers, subscriptions, invoices, payments, credit notes)
- [ ] Status mix sane (subscribers/subscriptions/billing_mode)
- [ ] Financial totals: invoice total, balance due, payments, credit notes, ledger — **diff vs Splynx GL export**
- [ ] Duplicate safety (no dup `splynx_*_id`, no dup `access_credentials.username`)
- [ ] Auth readiness (active credentials with secret)
- [ ] Mapping coverage per entity_type

---

## 8. Cutover

- [ ] Freeze config changes in DotMac
- [ ] Final delta: `incremental_sync --hours=48 --execute`
- [ ] Set `SPLYNX_SYNC_ENABLED=false` (Splynx → read-only)
- [ ] `populate_radius_from_subs.py --execute` (final RADIUS reconcile)
- [ ] Switch operational traffic to DotMac
- [ ] Watch app / Celery / RADIUS auth logs

---

## 9. Manual / not-migrated (own these separately)

- [ ] **Payment tokens & mandates** — NOT migrated; customers re-add cards. Plan comms + grace period.
- [ ] **Admin users & roles** — recreate manually
- [ ] **Custom field definitions** — recreate (only values import)
- [ ] **Email/SMS templates** — rebuild
- [ ] **Leads / quotes / inventory / scheduling tasks** — no target models; export separately if needed
- [ ] **Live tickets** — archive-only, no active continuity
- [ ] **Pre-go-live config:** payment keys (`paystack_*`, `flutterwave_*`), SMTP/SMS, `default_tax_application`,
      currency, invoice/document sequences; confirm payment webhooks publicly reachable

---

## Rollback
No automated rollback. Strategy: keep the pre-import DB backup, keep Splynx **read-only (not deleted)**
through the stabilization window, revert traffic via DNS/LB if needed.
