# Account-credit invoice reconciliation

## Purpose

This operator repair addresses one narrow drift state: a completed Deposit
Account Credit intent produced a succeeded, settled payment and unallocated
credit, but an already-issued eligible invoice was not allocated that credit.
The repair never records cash, changes the deposit amount, or creates an
adjustment. It creates only the missing payment allocation and the paired
invoice/credit-consumption ledger evidence through the canonical owners.

## Authority and safety boundary

`financial.account_credit_invoice_reconciliation` owns the reviewed repair.
Its read-only preview binds the account, invoice, payment, settlement, and
deposit intent into a SHA-256 fingerprint. Confirmation requires a current
staff principal with `billing:invoice:update`, a reason, command identity, and a
bounded idempotency key.

The command fails closed unless all of the following remain true under lock:

- the invoice is active, financial, payable for the exact expected amount, and
  is the account's oldest eligible debt;
- every active invoice line is non-service, keeping subscription/prepaid repair
  under its existing owner;
- the payment is active, succeeded, unrefunded, unreversed, and belongs to the
  same account and currency;
- the completed intent names that payment and retains the server-owned
  `account_credit_deposit`, `credit_only`, `pay_eligible_invoices` policy;
- the settlement and its unallocated ledger evidence equal the expected amount;
- canonical account credit and selected-payment room both equal that amount;
- no active allocation already consumes the payment for the invoice.

Confirmation enters `execute_owner_command` once. The account-credit
application participant creates the deterministic allocation and paired ledger
entries and settles the invoice. The same transaction records audit and outbox
event evidence. Replay returns the recorded allocation; stale or ambiguous
evidence is rejected.

## Operator workflow

Run `scripts/billing/reconcile_account_credit_invoice.py` without `--apply` and
review the JSON evidence. Apply only the same identifiers, amount, currency,
and returned fingerprint, adding the authorized staff ID, actor, command ID,
reason, and a unique idempotency key. A changed fingerprint requires a new
review. Raw SQL, payment re-recording, invoice balance overrides, and generic
adjustments are not valid fallback paths.
