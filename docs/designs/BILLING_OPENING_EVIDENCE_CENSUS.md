# Billing opening evidence census

`financial.invoices.opening_export` owns the read-only M8 evidence census through
`preview_billing_opening_export(db, *, query=PreviewBillingOpeningExportQuery(...))`.
The incumbent `financial.invoices`, Payments, Credit Notes and customer-subledger
owners retain their mutations and financial interpretations. This is a source
query, with no adapter, operational export, Starter import, apply option,
activation, permission, scheduler, schema or data repair.

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
