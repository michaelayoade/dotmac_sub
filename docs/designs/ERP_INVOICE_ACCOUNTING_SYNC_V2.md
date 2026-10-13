# ERP Invoice accounting-sync v2

Status: shadow-only compatibility contract. It does not authorize an ERP
cutover, a data repair, or a billing writer change.

## Released Billing fact transport preparation — 2026-10-13

Candidate implementation only; hosted acceptance is pending. The existing ERP
pull feed, invoice-sync contract, permissions and runtime delivery remain
unchanged. This slice adds no ingress, HTTP client, credential, dispatcher,
Billing outbox bridge or transport activation and does not depend on WHT work.

`app.services.dotmac_erp.billing_accounting_fact` maps the already pinned,
released Billing a1 `AccountingFactV1` into `BillingAccountingExportV1` with
`contract="billing.accounting.export.v1"` and
`fact_contract="billing.accounting.fact.v1"`. The complete canonical fact,
explicit tenant scope, source system, fact UUID/version, account and
posting-group identities, opaque source reference/authority, event and commit
instants, signed lane effects, allocation/deallocation/reallocation/refund/
reversal evidence and offsets, tax snapshots and FX provenance are preserved.
Money carries exact decimal amount, currency and minor units; invalid precision,
nonfinite values, floats, missing scope and naive timestamps fail closed.
Sub is tenant-only under ADR 0009: both mapping and captured-fact parsing refuse
platform scope; every accepted fact requires an explicit tenant UUID.
The transport digest covers every released fact field; it is a transport
canonicalization digest, not an assertion about Billing's internal digest
implementation. No GL classification, recognition decision, document identifier,
sequence or accounting relationship is inferred from `source_ref`.

The existing ERP owner has a dedicated, dormant
`stage_committed_billing_fact` flush-only participant and additive
`ErpBillingFlow.accounting_fact`. It requires an active registered owner command,
which proves transaction authority only. A future separately reviewed consumer
must obtain committed Billing outbox evidence and validate source and scope
before staging. Captured JSON does not establish that trust. No runtime caller
or consumer is wired in this slice. The participant uses the existing export
ledger and unique key; transaction-advisory serialization protects the absent
first insert before row lookup and canonical payload comparison. Replay identity
is `(source_system, exact_scope, fact_id, fact_version)`, independent of delivery
status and command/correlation IDs. Exact replay returns the existing export;
changed complete evidence refuses, including concurrent first inserts. Generic
invoice staging refuses accounting-fact-shaped payloads instead of inventing
invoice lines. ERP delivery and acknowledgement keep their existing owner.

`preview-module-accounting-export --fact-file <captured-fact.json>` calls the
actual parser and mapper before opening any database session. It emits a
captured-preview-only envelope, never invokes staging and provides no trust
promotion or live receiving endpoint.

Migration `688_billing_accounting_fact_export` adds one enum label, no data.
Its actual predecessor is `686_field_lifecycle_router_health_merge`, the complete
Sub graph's terminal revision at base
`f3a18e7ff2d89821eb410760bd326253fb5c36af`; the existing filename/revision
mismatch is preserved. Revision 434 stays frozen. The hosted migration canary
archives that immutable old source, migrates its real old model/enum chain in
an explicitly disposable database, asserts the new label is absent, then runs
the candidate upgrade and preserves an old export. The hosted integration
checkout includes full history and verifies that this immutable source is an
ancestor of the candidate before the canary runs. This avoids describing a
current-metadata predecessor rebuild as evidence of the old enum. Current-main
revision collision and all hosted acceptance checks remain required before
publication; no migration has been executed by this implementation session.

## Purpose and authority

Self-Care owns the Invoice document, line, discount, lifecycle, and issued tax
snapshot facts. Dotmac ERP owns chart-of-account mappings, tax-code mappings,
journals, tax transactions, and financial statements. The version-2 feed is a
read-only resolver between those owners; it does not post accounting and it
never changes an Invoice.

The endpoint is `GET /api/v1/invoices/accounting-sync/v2`. It is additive.

Authorization: the v2 endpoint accepts EITHER the existing
`billing:invoice:read` permission OR the narrower
`integration:accounting_sync:read` scope (for a future ERP accounting-sync
machine principal — kept out of the ordinary role builder, see
`scripts/seed/seed_rbac.py`). Holding `integration:accounting_sync:read`
alone does not grant access to the legacy `GET /api/v1/invoices/sync` feed,
which keeps requiring `billing:invoice:read` only and gained no new scope.

The legacy feed's authorization is unchanged; its query surface is not: both
`GET /api/v1/invoices/sync` and `GET /api/v1/invoices/accounting-sync/v2` now
also accept the optional `after_updated_at`/`after_id` keyset-cursor pair
described below.

The durable `integration.dotmac_erp_billing_adapter` outbox remains the target
cross-application boundary under ADR 0007. This pull feed exists to stop the
current implicit reconstruction from hiding whether an invoice is postable and
to support a controlled migration to that target.

## Contract

Every item carries `contract_version = invoice-accounting-sync.v2`, a stable
`updated_at,id` position, source classification (`native` or `splynx_legacy`),
the customer/account identity, lifecycle fields, header money, active lines,
and one disposition:

- `ready`: the stored header agrees with the projected active-line facts.
- `blocked`: ERP must not post the row; `issues` names every known contradiction.
- `not_applicable`: draft or pro-forma evidence is visible but is not an
  accounting document.

Header money is named to remove the version-1 discount ambiguity:

`subtotal_before_discount - discount_amount + tax_total = total`

Each line exposes its stored `source_amount`, calculated
`net_amount_before_discount`, `tax_amount_before_discount`, and
`gross_amount_before_discount`, plus the exact source tax-rate id, code,
percentage, active state, and `tax_application` snapshot. Inclusive tax is
extracted from `source_amount`; exclusive tax is added to it; exempt tax is
zero. The calculations use the same owner helper and minor-unit rounding as
Invoice total recalculation.

Tax-rate code, percentage, and active state are copied to additive versioned
InvoiceLine snapshot columns whenever the invoice owner creates or reprices a
line. The projection never dereferences today's mutable TaxRate row. Existing
taxed lines are not backfilled from current configuration because that would
manufacture historical evidence; they remain visible and blocked until a
separately approved evidence-based repair exists.

## Fail-closed issue vocabulary

The stable codes are:

- `no_active_lines`
- `line_amount_mismatch`
- `missing_tax_rate_reference`
- `tax_snapshot_missing`
- `header_subtotal_mismatch`
- `taxed_header_without_line_tax`
- `header_tax_mismatch`
- `header_total_mismatch`
- `legacy_header_totals_missing`
- `discount_allocation_undefined`

An issue can include a source `line_id` and expected/actual money evidence. It
does not include customer personal data or a free-form payload.

`taxed_header_without_line_tax` is the explicit form of the observed failure in
which the active lines project zero tax while the authoritative header contains
tax. `legacy_header_totals_missing` distinguishes imported Splynx archive rows
whose zero subtotal/tax headers do not describe their non-zero lines and total.
Neither condition is silently corrected by the read model.
`tax_snapshot_missing` identifies a taxed legacy line for which Self-Care did
not record the tax configuration at authoring time.

## Discounts

Invoice discounts are stored at the header. The current owner calculation
reduces the net subtotal and scales the aggregate tax, but it does not persist a
line or tax-group allocation. The governing tax design explicitly leaves the
apportionment and rounding-residual rule open. Version 2 therefore returns the
exact header discount and pre-discount line facts but marks every positive
discount `discount_allocation_undefined` and `blocked`.

This is intentional containment. A later version may become postable only
after finance approves and documents a deterministic group allocation rule,
including which group receives rounding residuals, and focused tests prove the
sum of projected group bases and tax equals the immutable header.

## Paging, freshness, and replay

The feed uses an inclusive `updated_since` filter and stable ascending
`updated_at,id` ordering. An owner update can therefore be replayed without
guessing. Account, status, active-state, limit, and offset filters are typed and
bounded. A typed `invoice_id` filter permits one explicit operator replay
without rewinding the global cursor or scanning another customer's invoices.
The query takes no locks and writes no data.

Paging supports two modes, both accepted by `GET /invoices/accounting-sync/v2`
(and the legacy `GET /invoices/sync` feed, which shares the same underlying
`apply_sync_page` helper):

- **Offset** (default, unchanged): `limit`/`offset`, as above.
- **Keyset cursor** (additive, optional): `after_updated_at`/`after_id`,
  supplied together — never one without the other (HTTP 422 otherwise).
  Paging advances by `(updated_at, id) > (after_updated_at, after_id)`
  instead of `OFFSET`, so a concurrent update to an unrelated row cannot
  re-sort it across a page boundary and skip a row the walk has not reached
  yet — the concrete hazard offset paging has under concurrent writes.

Revision semantics under the keyset cursor: `updated_at` is mutable, so the
cursor cannot promise "each row exactly once" across a walk that overlaps
concurrent writes — a row genuinely modified after being observed legitimately
reappears with its new revision later in the walk, and this is intended, not
a bug (ERP is idempotent on the source invoice id plus its source
`updated_at`). What the cursor does guarantee is narrower and is the actual
fix: a row whose own `(updated_at, id)` never changes during the walk is
never skipped.

ERP must treat `blocked` as a durable data outcome, not as a transient exception:
record the issue keyed by source invoice and source revision, advance the pull
cursor after recording it, and retry only after `updated_at` changes or an
operator explicitly requests replay. Transport, authentication, and database
availability failures remain run-level failures and must not advance the cursor.

## Shadow and cutover gates

Before ERP can consume version 2 for posting:

1. Compare version 1 and version 2 over a named non-production cohort.
2. Persist every blocked result in ERP and prove one permanent bad row cannot
   prevent later rows from advancing.
3. Prove ready rows create the same AR, revenue, output-tax, and reversal totals
   expected from the source facts.
4. Validate exact source tax code/rate/application mappings against one effective
   ERP sales tax code with its collected-tax account.
5. Measure the blocked population by issue code and obtain finance decisions for
   any repair or legacy exclusion.
6. Keep the version-1 endpoint and all billing writers unchanged until the
   shadow report is approved.

Migration `580_invoice_line_tax_snapshots` adds only nullable columns and a
`NOT VALID` snapshot-shape check, which still enforces every new or changed row
without scanning the historical table during deployment. It takes a five-second
lock timeout, performs no backfill, and leaves legacy rows untouched. Downgrade
refuses once any snapshot has been recorded so application rollback retains
financial evidence. A later low-traffic maintenance change may validate the
constraint after the legacy cohort has been measured.

## Canonical content digest (`digest_version`/`projection_digest`)

Sub is the sole owner of the invoice-accounting-sync feed's content-identity
digest: the connector and ERP previously each computed their own fingerprint
independently and disagreed, and one of the two guesses folded the nested
subscriber profile into the fingerprint even though a subscriber-profile edit
never advances `invoice.updated_at`. This document covers Sub's half only —
Sub now computes and publishes both fields via
`app.services.dotmac_erp.invoice_sync_digest.compute_invoice_projection_digest`.
Changing the connector and the ERP shadow task to forward this digest verbatim
instead of computing their own is **separate, not-yet-shipped follow-up work**
in those repositories; nothing in the connector or ERP has changed yet.

`digest_version` (currently `1`) and `projection_digest` (a lowercase sha256
hex string, rejected — not normalised — by the schema validator if malformed)
are additive fields on `InvoiceAccountingSyncRead`. The digest reuses this
repo's ADR-0064-governed canonicalisation primitives
(`app.migration_source.canonical`) rather than an ad hoc encoder.

**Covered-fact domain (version 1)** is every field of
`InvoiceAccountingSyncRead` except two deliberate exclusions:

- The entire nested `account`/`InvoiceSyncAccountRead` object (subscriber
  profile — name/email/phone/address/status/category). None of it is an
  invoice fact, and none of it advances `invoice.updated_at`, so including it
  produced a false "same revision, different projection" contradiction. Only
  the flat `account_id` UUID reference is covered.
- `updated_at` itself. It is the external revision key the digest is compared
  against per-key, not digest content; including it would make digest drift
  trivially "explained" by the key changing and add no signal.

Everything else — header totals, discount facts, timestamps, memo,
disposition, and the `issues`/`lines` collections (each reduced to a sorted
tuple of per-item canonical forms) — is covered. Changing the covered-fact set
or its encoding is an explicit cutover: bump `INVOICE_PROJECTION_DIGEST_VERSION`
and treat it as a new contract, per ADR-0064.
