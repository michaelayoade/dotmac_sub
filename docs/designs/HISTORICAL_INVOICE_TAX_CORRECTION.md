# Historical Invoice Tax Correction

## Decision

`financial.historical_invoice_tax_corrections` owns the exceptional,
Finance-reviewed correction of one paid invoice that omitted VAT. Existing
issued invoice lines remain immutable. A tax-only line, generic adjustment,
raw database update, or multi-commit operator sequence is not a substitute.

The correction is deliberately narrow. One preview must prove all of the
following for a single customer and currency:

- the source invoice is active, paid, base-only, and funded by one exact native
  payment allocation with exact invoice-credit evidence;
- the selected payment is succeeded, unrefunded, unreversed, and has no other
  active allocations;
- the customer's current VAT policy and active TaxRate support the correction;
- the VAT-inclusive replacement is either constructed from a separate voided
  Finance evidence draft or reuses one named pristine VAT-inclusive draft whose
  immutable tax snapshot exactly matches the source subtotal;
- when a historical allocation is missing its settlement or non-position
  consumption link, the payment owner can reconcile only the uniquely matched
  source-invoice credit and unallocated-credit rows; the preview binds those
  exact ledger IDs and the correction command repairs the missing structural
  consumption link without changing customer position;
- an existing-draft correction uses the draft's own issue and due dates and
  does not require a separate unpaid subscription invoice;
- the expected remaining payment-backed customer credit is exactly
  `payment amount - replacement total` and is recorded in the preview;
- the source, replacement, selected payment, Finance ticket, approver, tax rate,
  issue/due evidence, and exact residual are fingerprinted.

The original construction mode also proves the following additional evidence:

- the subscription invoice is one pristine positive draft;
- a separate, already-voided and never-funded Finance draft proves the intended
  installation description, taxable base, tax rate, and gross total;
- the selected payment is succeeded, unrefunded, unreversed, and has no other
  active allocations;
- current payment-backed customer credit equals exactly the subscription total
  plus the missing tax; and
- the current customer policy is taxable and the selected active TaxRate still
  matches the immutable snapshot on the voided Finance draft.

## Atomic outcome

Both confirmation commands enter `execute_owner_command` once and perform their
mode-specific steps in one transaction.

The original construction mode performs these steps:

1. `financial.invoices` voids the incorrect source invoice and releases its
   exact payment allocation through append-only reversal evidence.
2. `financial.invoices` issues the named subscription draft with explicit
   manual due-date provenance.
3. `financial.account_credit_applications` settles that invoice from the named
   payment only.
4. `financial.invoices` constructs and issues a new full installation invoice
   with the reviewed VAT rate and snapshot.
5. `financial.account_credit_applications` settles the replacement from the
   same payment only.
6. The replacement records typed lineage to the source invoice and line, source
   closure, voided Finance evidence, subscription invoice, payment, tax rate,
   and both new allocations. Audit and a PII-free domain event are staged in
   the same transaction.

The command succeeds only when both invoices are paid and both the selected
payment availability and the customer's spendable credit are exactly zero.
Any intermediate failure rolls the entire correction back.

The existing-replacement mode performs these steps:

1. `financial.payments` reconciles the exact source allocation credit and
   unallocated payment-credit ledger rows when the historical payment lacks a
   `PaymentSettlement` record.
2. `financial.payments` attaches or reconstructs the exact non-position
   consumption debit required to release the old allocation. A reconstructed
   row is an append-only structural pair with `affects_customer_position=false`;
   the payment owner audits that it has no money effect.
3. `financial.invoices` previews and voids the incorrect paid source document,
   releasing its exact payment allocation.
4. `financial.invoices` issues the named existing VAT-inclusive draft using its
   existing issue and due dates.
5. `financial.account_credit_applications` settles that invoice from the named
   payment only.
6. The invoice records typed source, closure, allocation, payment, VAT, ticket,
   approver, timestamp, residual-credit, and preview-fingerprint lineage. Audit
   and event evidence are staged in the same transaction.

The existing-replacement mode succeeds only when the replacement is paid, the
selected payment's available amount equals the fingerprinted residual, and the
customer's spendable account credit equals that same residual. For the reviewed
NGN 217,625 payment and NGN 215,000 replacement, the required residual is NGN
2,625. Any mismatch rolls the complete correction back.

## Locking, replay, and drift

The owner locks the customer account first, then the reviewed invoice IDs in
UUID order, their active lines and source allocation, followed by the selected
payment, exact ledger evidence, and tax rate. It recomputes the complete preview
under those locks.

One 16-to-120-character idempotency key reserves the resulting replacement
invoice. Child invoice-void keys are derived from it. A replay returns the same
closure, replacement, and allocations only when the stored preview fingerprint
and typed lineage still agree; conflicting or incomplete evidence fails closed.

## Operator boundary

Each CLI is preview-only unless `--apply` is explicitly supplied with the exact
preview fingerprint, command UUID, actor, active staff UUID with
`billing:invoice:update`, idempotency key, reason, and every reviewed document,
payment, tax, and approval identifier. The existing-replacement CLI is
`scripts/billing/correct_historical_invoice_tax_using_existing_replacement.py`;
the adapter owns only argument parsing, permission resolution, serialization,
and session lifecycle.

## Rollout

This is an additive command with no schema change. It must follow the normal
feature-branch, CI, immutable-image, staging-acceptance, and production
authorization sequence. Production execution is a separate explicitly
authorized action against a named host; merging this change does not execute a
correction.
