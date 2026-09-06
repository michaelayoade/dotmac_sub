# ERP Invoice accounting-sync v2

Status: shadow-only compatibility contract. It does not authorize an ERP
cutover, a data repair, or a billing writer change.

## Purpose and authority

Self-Care owns the Invoice document, line, discount, lifecycle, and issued tax
snapshot facts. Dotmac ERP owns chart-of-account mappings, tax-code mappings,
journals, tax transactions, and financial statements. The version-2 feed is a
read-only resolver between those owners; it does not post accounting and it
never changes an Invoice.

The endpoint is `GET /api/v1/invoices/accounting-sync/v2`. It is additive and
uses the same `billing:invoice:read` permission as the existing
`GET /api/v1/invoices/sync` feed. The existing feed stays unchanged during
shadow validation.

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

## Fail-closed issue vocabulary

The stable codes are:

- `no_active_lines`
- `line_amount_mismatch`
- `missing_tax_rate_reference`
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

No database migration is required for this Self-Care slice.
