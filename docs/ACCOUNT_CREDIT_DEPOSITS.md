# Deposit Account Credit

Sub owns this lifecycle. It is account credit, not a wallet, and it has no CRM
runtime dependency.

## Contract and owners

`financial.account_credit_deposits` owns eligibility, preview, typed intent,
provider correlation and atomic settlement. Every new deposit intent persists:

- `purpose=account_credit_deposit`
- `allocation_policy=credit_only`
- `credit_application_policy=pay_eligible_invoices`
- `policy_version=1`
- the account, amount, currency, preview fingerprint, provider/reference,
  idempotency key, channel, creator and resulting payment link

Legacy payment intents remain untyped and retain their existing
invoice-first-then-credit behavior. They are not backfilled or reclassified.

`financial.payments` owns the confirmed payment, exact settlement, unallocated
credit evidence, refunds and reversals. The deposit command disables automatic
invoice allocation and prepaid-renewal drawdown, so the whole receipt first
becomes evidenced credit.

The receipt presents gross cash received separately from the settlement value
credited after provider fees, invoice applications, any settlement-owned
prepaid application, and the remaining payment-backed credit. A receipt does
not promise service duration. When the downstream renewal owner actually funds
a period it publishes the exact `prepaid_service.renewed` outcome; portal and
notification views display that owner-provided renewed-through date.

`financial.account_credit_applications` then locks the account, chooses eligible
invoices and payment-backed credit deterministically, and invokes the payment
allocation preview/confirmation owner. It never constructs allocation or ledger
rows itself. Invoice issuance and deposit settlement call the same applicator.

## Eligibility and race policy

A customer may create a deposit only for an active, non-disabled,
non-cancelled subscriber account, in NGN, inside configured limits, with no
eligible payable invoice and no pending account-credit deposit. Blocked or
suspended billing accounts may deposit, but the deposit alone does not restore
access.

If an invoice appears after intent creation, confirmed cash is accepted and the
new credit is immediately applied to eligible invoices. Duplicate callbacks and
dead-letter replay return the existing payment. Provider amount, currency,
provider and account must match the server-owned intent.

Eligible invoices are active `issued`, `partially_paid` or `overdue` invoices
with a positive balance. Draft, void, written-off and incompatible-currency
invoices consume nothing. Oldest due debt wins; creation time and ID are stable
tiebreakers. Partial credit leaves an invoice partially paid. Only a fully paid
invoice reaches the existing entitlement/access owner.

## Refunds, reversals, void and access

Existing payment refund and reversal owners remain authoritative. They use
append-only evidence, consume unallocated credit first, reopen affected
receivables when allocated cash is returned and hand access state back to the
canonical access owner. Invoice void composes the account-credit owner to retire
typed account-credit allocations, append exact ledger reversals and restore the
same payment-backed credit. Other direct-payment or credit-note settlement must
still be reversed through its own owner before voiding. No balance counter is
edited.

## ERP accounting projection

ERP continues to pull the bounded payment sync feed. Deposit payments now carry
their settlement plus the typed intent purpose/policies. Later account-credit
allocations touch the parent payment watermark and include the exact invoice
credit and account-credit-consumption links, so incremental ERP sync observes
both stages.

Expected ERP journals are:

- Deposit confirmed: Dr Cash / provider clearing; Cr Unapplied customer credit
  liability.
- Credit applied: Dr Unapplied customer credit liability; Cr Accounts
  receivable.

Invoice revenue and tax recognition remain invoice-issuance concerns. ERP owns
the journals; Sub owns the customer receipt, credit and allocation evidence.
An ERP projection failure must be retried downstream and never rolls back Sub's
settlement.

## Operations

`AccountCreditApplications.inspect_invariants` is read-only and its aggregate
count is published by the existing billing-health snapshot. It reports payable
invoices with unused compatible credit, overallocated payments, completed
deposit intents without exact settlement evidence, duplicate provider
references and unresolved deposit webhooks. Repair invokes the canonical
applicator; it never invents payments or infers cash from memo text.

Customer-facing pages say “Deposit Account Credit”, show current credit and
payable-invoice eligibility, direct customers with payable invoices to the
ordinary invoice payment flow, and render an unresolved balance as unavailable
rather than zero.
