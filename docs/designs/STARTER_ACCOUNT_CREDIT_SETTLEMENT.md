# Starter account-credit settlement

## Decision

Eligible admin and scheduled postpaid drafts continue through Starter even
when the customer has settled account credit. The issuance transaction now:

1. locks the customer account and reserves exact payment-backed credit while
   the invoice is still a draft;
2. issues the receivable in `dotmac-billing` and records the
   `StarterInvoiceLink`;
3. accepts each reserved slice into Starter's `available_credit` lane and
   immediately applies it to the new Starter document;
4. records the matching canonical Sub `PaymentAllocation` and consumption
   ledger evidence; and
5. commits the Starter writes, Sub/ERP evidence, invoice mirror, audit, and
   events together.

Any refusal rolls back the entire transaction. There is no post-eligibility
fallback to legacy invoice issuance.

## Authority and invariants

- `financial.starter_issuance` owns coordination of the issuance-time bridge.
- `financial.account_credit_applications` owns source selection, locking,
  reservation fingerprints, and the exact Sub payment allocation.
- `dotmac-billing` owns the receivable and available-credit positions.
- The Sub allocation is settlement and ERP evidence; it is not a second
  receivable authority.
- Every Starter transfer key derives from the immutable Sub
  `PaymentSettlement` and invoice identities. The existing payment-plus-invoice
  allocation identity prevents a second legacy consumption.
- Only exact succeeded, active, same-account, same-currency payment settlements
  may cross the bridge.
- The Starter credit lane must resolve to `available_credit`; configuration of
  another lane fails closed.
- Full credit produces a paid invoice; partial credit produces a partially paid
  invoice with the exact remainder; no credit leaves the invoice issued with
  its full balance.

## Refunds and reversals

An applied Starter credit cannot be undone only in Sub: doing so would restore
legacy reusable credit while leaving the Starter receivable discharged.
Payment refunds therefore fail closed when an active allocation funds a
Starter-linked invoice. A future refund/reversal slice must add and prove the
inverse Starter credit-application command before enabling that operation.

## Freshness, drift, and repair

The bridge has no asynchronous projection window: both ledgers share one
database transaction. Drift is signalled by any mismatch among the active
`PaymentAllocation`, its consumption ledger entry, the `StarterInvoiceLink`,
and Starter's current position. Replay uses the same settlement/invoice source
identity. Repair must be owned by a reviewed financial command; adapters must
not write either ledger directly.

## Verification

The migrated-PostgreSQL issuance proof covers full and partial existing credit,
Starter collectible and residual-credit lanes, paid/partially-paid Sub mirrors,
the exact payment allocation, exhaustion of legacy reusable credit, and the
refund refusal. Policy and eligibility unit tests cover deterministic settings
and removal of the former recurring-credit legacy routing rule.
