# Financial ownership retirement inventory

Owner: Dotmac Sub architecture and the existing financial concern owners in
`app/services/sot_registry/domains/financial_access/`.

Status: interim source ratchet, 2026-10-14. No financial caller, invoice/payment
writer, money authority, or production activation changes in this slice.
The legacy ledger remains the sole live money authority for the coupled
replacement described by the current Sub-first cutover plan. New ledger data
must not be treated as available funding before that reviewed switch. This
inventory does not assert that paid-status business authority is retired.

The ownership declarations remain in the executable SOT registry; this document
records migration debt and enforcement, not a parallel service contract.
ADR 0007 remains the target architecture. Current migration decisions and actual
release/cutover status are recorded in canonical knowledge
`sub-first-billing-credit-draw-foundation-20261013` (v62 at this inventory).

## Exact source corpus

`tests/architecture/test_financial_ownership.py` preserves the existing writer
assertions and adds a paid-status read guard over every Python file under `app`.
`tests/architecture/invoice_paid_read_baseline.json` records the exact relative
file, qualified function, predicate kind, normalized predicate, original count,
remaining count, state, classification, owner/context, and reason. Identity is
independent of line numbers. Duplicate predicates in one function retain their
occurrence count, so copying an existing gate is growth.

The inaugural static census contains 117 identities / 123 occurrences:
110 identities / 111 occurrences are business debt; seven identities / twelve
occurrences are narrow report projections. These are source measurements, not
runtime or database acceptance. Every entry is initially active.

| Business area | Existing reads requiring replacement or explicit retirement |
| --- | --- |
| Entitlements and anchors | `service_entitlements`, `billing_automation`, prepaid calendar/coverage reconciliation, and cleanup/remediation |
| Customer money position | `customer_financial_ledger`, `customer_financial_position`, and renewal-consumption projections; these are business debt even when read-only |
| Prepaid renewal and historical correction | `prepaid_service_renewals`, draft/paid-period repairs, renewal origin/terms correction, quarantine and historical-tax correction |
| Deposit, checkout and Sales | `quote_deposits`, `sales/selfserve`, subscription-change settlement, gateway/direct-transfer/public/portal payment admission |
| Existing invoice/payment owners | Lifecycle guards, account-credit applications, settlement effects, closure, reconciliation, and opening-export classification; writer ownership grants no read exemption |

Exact report allowances are limited to `BillingReporting.get_overview_stats`,
`BillingReporting.get_dashboard_stats`, `billing_accounts._invoice_summary`,
`billing_health.paid_with_balance`, `reseller_portal.get_revenue_summary`,
`web_reports.get_regional_report_data`, and `tax_accounting._invoice_filters`.
The baseline names each owner and its report purpose. A new predicate or new
business function in these same modules still fails. Financial-position,
coverage, repair/admission, and customer action decisions are not report
allowances merely because their functions are read-only or return UI data.

Status presentation mappings and current ERP invoice projections serialize or
filter the requested document status without a literal paid business predicate.
They remain outside this predicate census. In particular,
`dotmac_erp/invoice_sync_projection.py` and `invoice_sync_digest.py` retain the
current feed; no new ERP transport or financial writer is introduced. A future
literal paid predicate in either module is measured and fails without an exact
reviewed corpus entry. There are no directory or filename exclusions.

## Shrink procedure

The initial ceiling digest freezes identity, original count, classification,
owner, and rationale. A second reviewed digest freezes the complete current
inventory, including remaining counts and retired states. The guard requires
the current source predicates to exactly equal active remaining counts.

Retire the runtime predicate and lower its `remaining_count` in the same source
change; set `state` to `retired` when that count reaches zero. Retain the entry
as a tombstone. Update only the approved inventory digest after review; the
inaugural ceiling digest stays fixed. Do not replace a predicate at the same
count, increase counts, remove tombstones, relabel business debt as reporting,
or reactivate retired entries. Any necessary detector correction requires
explicit inventory review and renewed evidence; digest edits are reviewable
source changes, not an approval mechanism that resists malicious test edits.

Future replacement readers must obtain typed settlement/coverage facts through
the owning contract, prove partial/reversed/refunded/replay/concurrency/rollback
behavior in hosted PostgreSQL tests, and activate with the coupled
invoice/payment/allocation ledger switch. Do not introduce an early empty-ledger
reader, dual money writer, legacy fallback, or account cohort in this slice.

## Detector boundary and validation

The scoped #3605 billing revert retires the paid-status predicate in
`backdated_service_invoicing._adjacent_invoice_charge_resolution` together with
that creation owner. Its inventory entry remains as a tombstone with zero
remaining reads; the original identity, classification, count, rationale and
ceiling digest remain unchanged. The approved inventory digest reflects this
retirement. Census paths use repository-relative POSIX spelling on all hosts.

The AST guard recognizes explicit `InvoiceStatus.paid` (including enum import
renames/value forms), paid strings on invoice status receivers, comparisons,
membership, SQLAlchemy status filters (including `filter_by(status=...)` on
recognizable Invoice queries and explicit invoice-enum keyword values), lexical
value/collection aliases,
`getattr(invoice, "status", ...)`, annotated Invoice parameters, and
`aliased(Invoice)` receivers. Loop and comprehension targets recognize
invoice-named receivers and known Invoice query/annotated iterable sources;
explicit Payment annotations and query sources exclude paid-string reads.
Explicit invoice enum predicates also identify
unpacked status values. Assignments, comments, docstrings, and display enum
mappings are not predicate reads. Payment and expense status predicates do not
become invoice reads merely because their string is `paid`.

This is bounded lexical analysis, not whole-program type inference or a SQL
parser. Dynamic/reflection-built predicates, cross-module value propagation,
complex control-flow rebinding, helper-return status inference, dynamic SQL,
SQL text aliases/schema forms beyond the recognized literal invoice table, and
untyped non-invoice-named receivers with only string statuses need separate
review. Predicate normalization can require review after an alias refactor;
line moves do not. The guard does not prove financial correctness or retirement
of every indirect status dependency.

Hosted-CI canaries are authored for new business reads, aliases, strings/enums,
SQL filters/text and keyword filters, loop/comprehension receivers with Payment
negative controls, duplicate growth, line moves, same-count identity replacement,
classification edits, stale entries, retired-entry restoration, legitimate exact
report reads, and a business gate added inside a report module. Local validation
is limited to pinned Ruff 0.15.1, AST source inventory, JSON/digest inspection,
and static diffs. Runtime tests, application imports, database checks, dependency
installs, and full architecture execution belong to hosted CI.
