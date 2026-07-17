# Splynx retirement decision and cutover

Status: approved retirement of the import archive (Tier 1), 2026-07-17.
Tier 2 (legacy-BSS id mapping and billing-transaction reconciliation) is
assessed here but **not** retired; it needs an explicit decision.

## Authority change

Splynx was the pre-migration BSS. Sub now owns subscribers, billing, and
support tickets natively — Splynx has had no write path into this system since
the migration completed, and the `app.services.migrations`,
`app.services.splynx_customer_sync`, and `app.tasks.splynx_sync` runtimes were
already removed.

What remains is import residue: read-only archive models whose tables were
never populated (or were purged after the import), plus a portal-onboarding
step counter carried over from the Splynx portal. They have no owner, no
writer, and no reader.

This revision removes the archive models and their tables.

## Preserved evidence

**None — and that is the material difference from
[VAS_RETIREMENT](VAS_RETIREMENT.md).**

VAS retained eight tables because they held real, immutable financial history.
Every Splynx table holds **zero rows** in production, verified read-only on
2026-07-17 against `selfcare.dotmac.io` (94.72.107.76, container
`dotmac_pg_local`):

| Table group | Rows |
|---|---|
| `splynx_archived_{tickets,ticket_messages,quotes,quote_items}` | 0 |
| `splynx_id_mappings` | 0 |
| `splynx_{customers,customer_info,customer_billing,customers_values,accounting_customers}` | 0 |
| `splynx_{invoices,invoice_items,payments,billing_transactions,mrr_statistics}` | 0 |
| `splynx_{tickets,ticket_messages,quotes,quotes_items}` | 0 |
| `splynx_{routers,services_internet,tariffs_internet,ipv4_networks_ip,locations}` | 0 |
| `splynx_{monitoring,monitoring_log,statistics,traffic_counter,inventory_items,admins,partners}` | 0 |
| `portal_onboarding_states` | 0 |

There is no history to preserve, so keeping the tables preserves nothing and
costs a permanently misleading schema: four `splynx_archived_*` tables that
imply an archive exists. Dropping is the honest outcome.

The emptiness is not assumed at deploy time. The cutover gate re-checks it
against the live database (below), so an environment that *does* hold rows
fails loudly instead of losing them.

## The `splynx_customer_id` carve-out — do not remove

`Subscriber.splynx_customer_id` **stays**, and is deliberately excluded from
this retirement. It is populated on **15,263 of 15,291 subscribers (99.8%)**
in production and is load-bearing today:

- `crm_portal.resolve_crm_subscriber_id` resolves the CRM link through it —
  a subscriber without it cannot be resolved to CRM at all;
- `subscriber.py`, `subscriber_growth.py`, `billing_payment_receipts.py`, and
  `mrr_snapshot.py` read it.

It is **provenance**: an external reference to the system a subscriber was
imported from, exactly like `crm_work_order_id` on `work_order`
(WORK_ORDER_IDENTITY_SOT). Provenance is not identity, it is never a join key
for native decisions, and it retires **with CRM** — not here. A future cleanup
that sees the word "splynx" and reaches for this column would break CRM
linkage for 99.8% of the base;
`tests/architecture/test_splynx_retirement.py` asserts its presence
positively so that mistake fails a test rather than a filing.

## Cutover gate

Revision `329_retire_splynx_import_archive` refuses to drop a table that holds
rows. It counts every target table first and raises with a per-table breakdown
if any is non-empty, listing what it found. Production is empty today; the gate
exists for every other environment and for the possibility that the fact
changes between this decision and the deploy.

Do not bypass the gate by truncating. If a table has rows, someone imported
data this decision did not account for — re-open the decision.

`alembic/env.py` already wraps `op.drop_table` to no-op when a table is absent,
so the migration is safe to re-run and safe on a fresh `001_squashed` database
where the models (and therefore the tables) never existed.

## Fallback

`downgrade()` recreates the four archive tables and `portal_onboarding_states`
empty. That is a faithful rollback precisely because there is nothing to
restore: the tables were empty when dropped. A code rollback to a release that
still registers the models will therefore find the schema it expects.

## Tier 2 — assessed, not retired

These are wired into live surfaces and are **not** part of this revision. They
are dead *at runtime* only because their tables are empty; the code paths
execute on every request that touches them.

### `splynx_id_mappings` — `legacy_bss.py`, `splynx_mapping.py`, `external_bss_adapter.py`

`legacy_bss` is not an archive. It is the **backing store for a live
subscriber attribute**: `Subscriber._legacy_bss_customer_id` is a staging
`ClassVar`, and a `before_flush` SQLAlchemy event listener writes every set
value into `splynx_id_mappings`. `get_customer_id()` reads back through it.
`deleted_import_clause()` and `get_effective_created_at()` also drive
subscriber filtering and reporting from `splynx_*` metadata keys.

Cost of removal: this is a live write path on the `Subscriber` flush cycle, and
`external_bss_adapter` is a declared boundary adapter with its own tests
(`tests/test_boundary_adapters.py`). Removing it means deciding that the legacy
external-BSS customer id is gone for good — which overlaps the
`splynx_customer_id` carve-out above and should be settled with it, at CRM
exit, not before.

Note the redundancy worth resolving *then*: `splynx_customer_id` (a column,
99.8% populated) and `splynx_id_mappings` (a table, empty) both model "this
subscriber's id in the legacy BSS". Two mechanisms, one fact — the column won.

### `splynx_billing_transactions` — `web_billing_ledger.py`

A reconciliation view: the ledger page cross-checks imported Splynx credits
against native `LedgerEntry` rows before a `_LEDGER_CUTOVER` date, surfacing
imported payments that never became ledger entries. With the table empty it
finds nothing.

Cost of removal: it is a **money-reconciliation surface**. If the import is
genuinely complete and reconciled, it is dead weight; if anyone still expects
to audit pre-cutover payments through this page, removing it removes the
evidence path. Two `scripts/one_off/` audits also read the model. Settle by
confirming the reconciliation is closed, then retire the view and the table
together.

### `web_admin_dashboard` sync-status tile

Reads `splynx_mapping.sync_status()` and renders "last sync / total mappings /
healthy". With zero mappings it permanently reports **unhealthy** — a red tile
for a sync that will never run again. It is already wrapped in a
`try/except`, so it is cosmetic, but it is actively misleading operators today
and is the cheapest Tier 2 item to remove independently.

## Enforcement

`tests/architecture/test_splynx_retirement.py` keeps the removed model paths
and their imports absent, keeps the retired tables out of the model registry,
and positively asserts the `splynx_customer_id` carve-out so it survives future
cleanups.
