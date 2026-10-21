# Billing opening evidence census

`financial.invoices.opening_export` owns the read-only M8 evidence census through
`preview_billing_opening_export(db, *, query=PreviewBillingOpeningExportQuery(...))`.
The incumbent `financial.invoices`, Payments, Credit Notes and customer-subledger
owners retain their mutations and financial interpretations. A read-only operator
CLI calls this source query; this change performs no operational capture. It adds
no financial-write or import adapter, Starter import, apply option, activation,
permission, scheduler, schema or data repair.

The caller supplies an already active, clean PostgreSQL READ ONLY transaction at
REPEATABLE READ or SERIALIZABLE isolation. The query checks actual transaction
settings and captures its snapshot identifier, database clock and Alembic heads.
It never begins or completes transactions, flushes, saves, commits, rolls back or
creates savepoints. Autoflush is temporarily suppressed and restored. A read-only
transaction also prevents previously flushed business writes from masquerading as
committed source evidence. The caller owns snapshot/session lifecycle and retries.

Population v1 includes all Subscriber identifiers, Invoice and InvoiceLine rows,
Payment, PaymentRefund, PaymentReversal, PaymentAllocation, CreditNote and
CreditNoteApplication rows, LedgerEntry rows, PrepaidFundingBaseline,
CustomerSubledgerOpeningPosition, PrepaidOpeningFundingConsumption rows and all
CustomerPostingGroup/CustomerPositionEffect rows visible in that one snapshot.
No lifecycle, creation-window, subscription, contract or obligation join filters
reduce this population. Unlinked lines, allocations, effects and financial account
mappings are retained and classified. Subscribers without financial activity are
explicit exclusions. This is the incumbent single-installation Sub database;
installation UUID/repository SHA/precision declarations are caller provenance,
not authenticated installation identity, tenant authorization or source proof.
No metadata, memo or external customer identifier supplies structural identity.
The adapter must establish the installation/database relationship before any
future use. V1 is not a multi-tenant export interface.

Mutable documents are current observations. Economic cutoff filters do not
reconstruct their earlier mutable values. Rows created, changed, or economically
effective after cutoff produce blockers and remain visible. Immutable position
comparisons reuse `resolve_position_evidence_at`, bounded by both economic and
recorded time. An effective durable runtime authority cutover is required;
shadow postings remain comparison evidence and never become opening authority.
Current snapshot completeness is separate from opening eligibility.

Invoice discharge uses the existing canonical settlement query, including
proportional partial refunds, credit-note applications and opening consumption.
The preview retains original issue/due provenance, structural lines/subscriptions,
settlement references, raw source amounts, each discharge component, stored and
canonical residuals. Due basis may remain unknown: that does not itself block AR
evidence eligibility. `collections_due_basis_verified` reports only the due-basis
gate, and cannot authorize Collections, which still needs its own full policy.
Historical settled transactions supporting an open item are supporting evidence,
not instructions to import those closed transactions.

Draft, void, inactive/proforma and fully closed documents are explicit exclusions;
contradictory nonzero residuals remain blockers. Missing mapping/provenance,
duplicate actual row/ledger identities, exact residual disagreement, negative residuals, unsupported
precision, shadow authority and unsupported/unbacked lanes are blocking exceptions.
Credit-note partial applications share a note/invoice legitimately; their actual
application and ledger identities remain distinct. Negative applications require
linked original ledger/application reversal evidence. Active settlement ledger
references must match account/currency, source kind, invoice/payment affinity,
direction and exact amount, including consumption evidence. No public owner
validator exposes this complete census check; the query verifies structural
affinity without calculating a second settlement position. Non-finite source
amounts fail capture with the typed `nonfinite_source` error before financial
comparisons or totals; no finite substitutes or completed controls are returned.

V1 supports the canonical two-decimal settlement owner's precision only. Credit,
reserved/consumed funding, write-off, refund and adjustment lanes stay distinct:
nonzero lanes have no reviewed opening mapping in v1 and block admission. A cash
balance never proves prepaid funding. No cross-currency/lane netting, rounding
tolerance, clamping or balancing plug is permitted. Conservative account/currency
blocking can quarantine an otherwise consistent invoice when another source in
that key is unresolved.

Controls include every invoice's total, canonical discharge and signed residual,
counts of eligible/excluded invoices, all blocking exception occurrences, and
source counts per account/currency, including unresolved keys. Global tuple counts
allow a consumer to reconcile every captured row; exclusions and exceptions may
overlap intentionally. Content SHA-256 covers canonically ordered typed evidence,
declared provenance and actual schema heads. The actual snapshot and capture clock
are retained in the capture envelope but excluded from content identity so unchanged
captures compare. The digest is drift evidence, not authentication or import approval.

Hosted unit and real migrated PostgreSQL tests cover historical/multiple/unlinked
items, refunds, exact controls, authority, stable snapshots, source drift and no
writes. Architecture guards keep this query free of adapters and transaction
completion and require reuse of the canonical settlement and lane resolvers.

## Population traversal

The census indexes invoice line and settlement references by invoice ID, and
invoice/source occurrences by the exact account/currency pair. Buckets retain
source order and duplicate occurrences, including unresolved account keys;
they do not use identity maps that could collapse rows. Controls reuse these
buckets, final exception counts and an eligible-ID lookup rather than scanning
each complete population for every partition. The existing settlement and
position resolvers, SQL populations, snapshot checks and wire fingerprints stay
unchanged. Canonical per-invoice/per-account resolver calls remain; this is not
evidence of a measured production export duration. Hosted parity and traversal
canaries cover the indexing change before a separately authorized capture.

## Read-only operator entry point

After an operator has an approved, explicitly named database target, the existing
application database configuration supplies that target to this source-only CLI:

```bash
python -m scripts.migration.billing_opening_export \
  --source-repository-sha <exact-40-character-source-sha> \
  --installation-id <installation-uuid> \
  --economic-cutoff <ISO-timestamp-with-explicit-offset> \
  --currency-precision NGN:2 \
  --population-version sub-billing-opening-census-v1 > opening-census.json
```

Repeat `--currency-precision` once per declared source currency; duplicates and
unsupported precision are refused before opening a session. All capture coordinates
are explicit caller declarations, not authenticated installation/source proof.
The canonical `app.db.read_only_snapshot_session` owns PostgreSQL READ ONLY,
REPEATABLE READ setup and rollback/close. The CLI calls the census owner, whose
clean-session and actual database snapshot checks remain authoritative. It performs
no import, repair, financial mutation or source-authority switch.

Standard output contains sorted-key JSON with exact decimal strings, UUID strings,
aware ISO timestamps and enum values. Unsupported or non-finite values fail rather
than becoming approximate floats or arbitrary strings. Exit status 0 means capture
completed without blocking exceptions, 1 means a completed census has blockers,
and 2 means invalid arguments or failed capture/serialization. A zero exit status
is evidence capture only; it never authorizes import, cutover or operational use.
Database errors emit only `snapshot_unavailable`, with no connection details.
This entry point has not been executed against any operator target by this change.

## V2 raw cash receipt evidence

The same operator entry point accepts explicit
`--population-version sub-billing-opening-census-v2`. V1 keeps its existing DTO,
selected population and content-fingerprint algorithm. V2 returns the inherited
AR evidence plus a separate `cash_evidence` object; its fingerprint covers that
object and the V2 capture coordinates. V1 AR eligibility is not cash qualification.
The CLI returns 1 when either the AR census or the separate cash evidence has
blockers. No raw cash row becomes qualified: `qualified_payment_ids` stays empty.

V2 observes complete, ID-ordered populations in the same clean read-only PostgreSQL
snapshot, without inner joins, lifecycle/date filters or event selection:

| Source | Retained evidence | Qualification boundary |
| --- | --- | --- |
| Payment | Structural subscriber/billing account/provider/external IDs, gross amount, fee, refunded amount, status/active state, paid/created/updated times, reserved purchase and import-run IDs | Status, external reference and fee do not attest external cash; purchase/import mappings remain unsupported |
| PaymentSettlement | Payment ID, origin, stored amount, unallocated/prepaid amounts, all three ledger IDs, fingerprint, idempotency key and creation time | Ordinary creation stores gross; verified-provider staging stores net. Neither value is normalized into the other |
| PaymentProviderEvent | All 0..N observations, exact provider/payment/invoice/external IDs, event type, source, status/observed status, gross/fee/net, currency/reference, effect, digest, error code and receipt/processing times | No raw signature or original body is persisted; verified status/normalized digest alone does not qualify cash |
| IntegrationInbox | Receipt/installation/binding/external event IDs, event type, payload digest, state, safe consequence provider-event/payment/provider IDs, error code and receipt/processing/created/updated times | Only the three known consequence-ID JSON scalars are selected; invalid IDs are blockers. No namespace mapping is inferred |
| Refund/reversal | Payment/provider-event IDs, ledger/billing-account-ledger/credit-consumption IDs, stored amount/currency/origin/fingerprint and creation time | Structural affinity is checked; downstream consumption, source trust and complete reversal meaning remain unqualified |
| PaymentProof/correction | Structural proof/payment/account IDs, claimed/gross/WHT/verified amounts, state/time, duplicate/original proof IDs, reversal/ledger IDs and correction fingerprints/keys | Staff verification and uploaded proof are observations, not independent bank confirmation |
| TopupIntent | Structural account/invoice/completed payment/provider/binding IDs, requested/actual amount, currency/external ID, status and completion/created/updated times | Reservations and consumption need a separately reviewed mapping |

Each table has an explicit source count, including orphan and unsupported rows.
Every retained row has a `cash_unqualified` blocker. Missing/crossed structural
payment, account, currency, provider, ledger and consequence links add blockers;
zero or multiple payment events are retained and marked ambiguous without choosing
one as proof. Rows whose timestamps cross cutoff remain captured and blocked.
Non-finite money or naive receipt timestamps fail the entire capture without a
substitute. Exact decimal strings, UUIDs, enum values and aware timestamps pass
through the existing strict CLI encoder.

Provider payloads, inbox payloads/headers, signatures, file locations, review notes
and free-form provider/inbox errors are neither selected nor exported. The safe
fields do not authenticate installation declarations or establish a trusted mapping
to Starter namespaces. No new bank-payout prerequisite is introduced.

Source extraction evidence at base `89360003fad9c261823b9d41fa24bd3340d3584f`:
`app/models/billing.py` owns receipt/payment/event/refund/reversal/topup columns;
`app/models/payment_proof.py` owns proof/correction columns;
`app/models/integration_platform.py::IntegrationInbox` owns receipt metadata.
`app/services/payment_webhook_commands.py::ProcessedPaymentWebhook.consequence`
and `ProcessedIntegratorSettlement.consequence` define the three exported
consequence-ID keys. `app/services/billing/payments.py` ordinary creation and
`stage_verified_provider_settlement` demonstrate the distinct gross/net paths.

Remaining evidence gaps are explicit: trusted external-cash/source attestation,
original signed bytes, live installation/namespace mapping, source-specific
external-event affinity, fee/net semantics, billing-account ledger meaning,
prepaid reservation/consumption, import-batch reversals and special recovery-run
qualification. The raw populations and identifiers support future review; this
slice performs no capture on an operator target, money mutation, import, repair,
namespace assignment or writer switch. Hosted PostgreSQL canaries own execution
acceptance; local static checks are not database or cash-qualification evidence.

## V3 current documentary and tax observations

The same owner and operator entry point accept explicit
`--population-version sub-billing-opening-census-v3`. V1 and V2 retain their exact
contracts, selected columns and fingerprint algorithm. V3 inherits V2 and adds
`documentary_observations`, covered by its separate version and content fingerprint.
Existing AR eligibility remains a separate classification, never documentary
qualification. The CLI returns 1 for any unavailable original-evidence record as
well as existing AR or cash blockers.

V3 selects every Invoice's current subtotal, discount amount, tax total, total and
billing-period bounds, and every InvoiceLine's current description, tax-rate ID,
tax snapshot version/code/percent/active flag and tax application. Both complete
populations are ID-ordered in the same PostgreSQL snapshot. Existing inherited
invoice/line IDs, dates, amounts, activity, quantities and unit prices remain
available for structural correlation. Non-finite new numeric observations fail
capture. These are current persisted observations; a persisted tax snapshot does
not authenticate the original document or establish its original applied tax.
Missing snapshots remain null. A null tax-rate ID does not mean zero VAT, exemption
or non-applicability. V3 never joins live TaxRate, party, branding, bank/settings or
PDF data to invent historical values, and never exports metadata or file locations.

Every invoice, including closed, draft or otherwise excluded rows, has an explicit
`unavailable_original_evidence` record. Its typed requirements name every mandatory
`OpeningOriginalDocumentV1` field: original number/issue instant, documentary
reference/digest, monetary totals, seller/customer and party tax identities,
applied tax snapshots and evidence reference, FX evidence reference, profile
code/version, payment instructions, brand asset, locale and timezone. The optional
FX snapshot is not asserted applicable or absent; required FX evidence is still
unavailable. Current values of even matching fields cannot satisfy these original
requirements without authentic frozen evidence. Historical-discharge source/live
namespace mapping, tax provenance, and withholding/non-cash replay bindings remain
separate named unavailable requirements. Raw proof WHT amounts or credit-note
applications never establish accepted withholding, non-cash entitlement or replay
identity. The two explicit reasons are missing authenticated frozen document and
missing qualified discharge/replay evidence. These conservative records report
what this exporter cannot establish, rather than asserting no external evidence
exists or zero historical discharge.

Source model evidence is `app/models/billing.py::Invoice` and `InvoiceLine`.
Incumbent PDF export metadata has no authentic original documentary digest;
Subscriber and TaxRate are current mutable records. Original document bytes,
frozen party/tax/FX/presentation provenance, qualified historical discharge and
source mapping, and external human validation remain prerequisites for any future
opening-import qualification. This slice performs no original-document recovery,
financial replay, source correction, external validation or authority change.
Hosted tests own runtime acceptance; local AST and pinned Ruff checks do not prove
PostgreSQL acceptance or original-document qualification.
