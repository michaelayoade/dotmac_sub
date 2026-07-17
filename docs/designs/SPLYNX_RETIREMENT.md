# Splynx retirement decision and cutover

Status: approved retirement, 2026-07-17. Tier 1 (import archive) and Tier 2
(legacy-BSS id mapping, the dashboard sync tile, and the billing-ledger
reconciliation view) are both retired here.

`splynx_billing_transactions` and `Subscriber.splynx_customer_id` are
deliberately **preserved** — see the two carve-outs below.

## Authority change

Splynx was the pre-migration BSS. Sub now owns subscribers, billing, and
support tickets natively — Splynx has had no write path into this system since
the migration completed, and the `app.services.migrations`,
`app.services.splynx_customer_sync`, and `app.tasks.splynx_sync` runtimes were
already removed.

What remains is import residue: read-only archive models whose tables were
never populated, a portal-onboarding step counter carried over from the Splynx
portal, and a legacy-BSS id-mapping path. They have no owner, no writer, and no
reader.

This revision removes them.

### The import never ran here

Production `pg_stat_user_tables` reports `n_tup_ins = 0` for
`splynx_billing_transactions`, `splynx_customers`, and `splynx_id_mappings`:
not "empty now", but **never written, not once**. The Splynx import was never
executed against this database. That is the evidence base for dropping rather
than preserving — there is no history here to lose, and there never was.

(It does *not* follow that the Splynx backups themselves are gone. See the
`splynx_billing_transactions` carve-out.)

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

Revision `330_retire_splynx_import_archive` refuses to drop a table that holds
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

`downgrade()` recreates the four archive tables, `portal_onboarding_states`, and
`splynx_id_mappings` (with its `splynxentitytype` enum) empty. That is a
faithful rollback precisely because there is nothing to restore: the tables were
empty when dropped. A code rollback to a release that still registers the models
will therefore find the schema it expects.

`splynx_id_mappings` owned a native PostgreSQL enum type, so `upgrade()` drops
`splynxentitytype` with its only user and `downgrade()` recreates it.

## Tier 2 — retired

### `legacy_bss.py` — unreachable, not a live write path

An earlier assessment recorded `legacy_bss` as "the backing store for a live
subscriber attribute", on the reasoning that its `before_flush` listener writes
`splynx_id_mappings` on every `Subscriber` flush. **That was wrong**, and the
correction is the reason this tier could be retired at all.

The listener registers *at import time*. **Nothing imports `legacy_bss`** — the
only reference in the tree was a comment in `app/models/subscriber.py`. Verified
by executing a full `import app.main` and inspecting SQLAlchemy's registry:
`app.services.legacy_bss` never enters `sys.modules`, and the app registers
**zero** `before_flush` listeners. The module could not run, and never did.

Removed with it: `Subscriber._legacy_bss_customer_id` (the staging `ClassVar`
its only reader used), `app/services/splynx_mapping.py`,
`app/models/splynx_mapping.py`, and the `splynx_id_mappings` table.

The redundancy noted in the earlier assessment resolves here: `splynx_customer_id`
(a column, 99.8% populated) and `splynx_id_mappings` (a table, never written)
both modelled "this subscriber's id in the legacy BSS". Two mechanisms, one
fact — the column won, and the table is now gone.

### `external_bss_adapter` — kept, splynx methods stripped

The adapter has a real non-Splynx purpose: `build_reference_payload` /
`sync_reference` handle generic external references and it is registered in
`adapter_registry` with its own tests. Only `register_splynx_mapping` and
`lookup_splynx_id` were Splynx-specific, and they had **no callers at all** —
not even tests. Those two methods are gone; the adapter stays.

### `web_admin_dashboard` sync tile — dead context, no tile

The earlier assessment called this "a permanently red tile". There is no tile:
`sync_status` was computed, placed in the dashboard context, and **never
rendered** — no template in the tree references it (the only `sync_status` in
`templates/` belongs to the unrelated OLT detail page). It was dead context, so
it is simply removed; there is no UI to redesign and nothing to fake healthy.

Removed with it: the orphaned `_network_monitoring_int_setting` helper and the
`dashboard_sync_healthy_age_seconds` setting (spec + seed), which configured
nothing once its only reader was gone. A knob that tunes a deleted computation
is worse than no knob. A stale `domain_settings` row may remain in existing
databases; it is inert.

### `web_billing_ledger` reconciliation view — removed

The ledger page cross-checked imported Splynx credits against native
`LedgerEntry` rows before `_LEDGER_CUTOVER`, display-only. With a table that was
never written it could only ever return an empty list, so the query and
`_splynx_credit_as_ledger_row` are gone. `_LEDGER_CUTOVER` **stays** — it also
bounds the invoice query and is not Splynx-specific.

## The `splynx_billing_transactions` carve-out — do not drop

The model and its table **stay**, and are asserted positively by
`tests/architecture/test_splynx_retirement.py`.

`scripts/one_off/billing_alignment_audit.py` and
`audit_void_mirror_double_reversals.py` read `SplynxBillingTransaction` as
money-adjudication evidence. The first documents running against a replica or
an isolated environment with "source tables loaded from retained Splynx
backups"; the second uses the Splynx mirror as **proof** that a void was already
absorbed into `subscribers.deposit` before it will soft-delete contra debit
ledger rows under `--apply`.

Empty in production is therefore **not** the same as "the data does not exist".
Dropping the table would remove the schema those retained backups load into, and
with it the ability to adjudicate pre-cutover money. It retires when that
reconciliation is confirmed closed — the view is gone, the evidence path is not.

## Out of scope: the orphan tables

Production holds roughly 28 further `splynx_*` tables (`splynx_customers`,
`splynx_invoices`, `splynx_payments`, `splynx_routers`, …) that have **no
models**. They were created by the original import tooling, not by Alembic, and
`001_squashed` — which builds from models — never creates them. No code
references them.

They are not dropped here. Dropping tables the ORM never knew is a different
risk class from retiring models, they may be the very tables the retained-backup
workflow restores into, and it deserves its own decision rather than riding
along with a code retirement.

## Enforcement

`tests/architecture/test_splynx_retirement.py` keeps the removed model and
service paths absent, keeps their imports and symbols out of `app/` and
`scripts/`, keeps the retired tables out of the model registry, and positively
asserts **both** carve-outs — `splynx_customer_id` and
`splynx_billing_transactions` — so a future sweep that reaches for either fails
a test rather than a regulatory filing or a money audit.

The import-linter contract "Application code must not import retired Splynx
migration runtime" (`pyproject.toml`) forbids `app.services.migrations`,
`app.services.splynx_customer_sync`, and `app.tasks.splynx_sync`. All three were
already gone before this retirement, so the contract guards nothing today. It is
kept deliberately: it costs nothing and it stops them returning.
