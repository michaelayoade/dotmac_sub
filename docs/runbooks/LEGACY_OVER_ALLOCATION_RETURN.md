# Legacy over-allocation return to account credit

Owner: `financial.legacy_over_allocation_correction` (permission
`billing:payment:update`). CLI: `scripts/billing/return_legacy_over_allocation.py`.
Events: `payment_allocation.over_allocation_returned`. Audit action:
`return_legacy_over_allocation_to_account_credit`.

Use this when a PAID invoice carries a legacy (Splynx-era) payment allocation
that pushes its settled amount above its total, and Finance has decided the
excess belongs in the customer's account credit. Example: INV-108193 (17,500,
paid) holds two allocations, 17,500 and 18,812.50, both from 2026-06-16
payments.

The existing reviewed reversal (`POST /payment-allocation-reversals/*`) cannot
do this. It refuses a legacy allocation ("Allocation lacks paired ledger
evidence") and is limited to void invoices. Do not work around it with SQL.

## What the owner proves

Every condition is checked read-only at preview and again under lock at
confirmation. Any failure is a named blocker and the command refuses.

- The allocation is legacy and active: no `ledger_entry_id`, no
  `consumption_ledger_entry_id`, no preview or idempotency evidence, and no
  reversal evidence.
- The allocation has real legacy provenance: its payment carries a
  `splynx_payment_id` or an `import_run_id`. Absence of ledger fields alone also
  describes Sub-native allocations (for example an allocation created in Sub by
  an admin edit after cutover), so it is not proof. A native allocation is
  refused (`allocation_not_legacy_provenance`) and Finance must handle it
  through a separate route; do not edit provenance fields to get past this.
- No other active payment on the account repeats the payment's receipt number,
  external id or a long reference / bank session id found in its memo. A match is
  refused (`possible_duplicate_payment_reference`): the transfer may be recorded
  twice and Finance must resolve which payment is real first.
- Its payment is an active, succeeded customer payment on the invoice's account
  and currency, with no refund, reversal, purchase reservation, or settlement
  row, and this is its only active allocation.
- The payment's whole amount is already carried by exactly one active,
  invoice-free ledger credit, and no account-credit consumption debit exists for
  the payment. That is the proof that the ledger already treats the money as
  account credit.
- The invoice is active, not proforma, `paid` with zero balance, and the
  allocation is exactly the excess: active allocations plus applied credit notes
  minus this allocation equal the invoice total. The invoice therefore stays
  fully paid by its remaining allocations.
- The operator restates the exact allocation amount, invoice total, and
  remaining settlement, and each must match the stored values.

## Effect

- The allocation is marked reversed (`is_active=false`, `reversed_at`, reason,
  actor, preview fingerprint, idempotency key). The invoice status, balance, and
  total are not recomputed or changed.
- **Ledger postings: none.** The payment's credit is already an unallocated
  ledger credit and the legacy allocation never consumed it. A reversal credit
  would count the same 18,812.50 twice. The preview shows
  `ledger.postings: []` and equal `account_credit_before`/`account_credit_after`.
- The payment's unallocated amount rises by the returned amount
  (`payment_allocation_unallocated.before`/`after`, an allocation-table view only that does not change account credit).
- Prepaid funding is document-based and does not change: same-account
  allocations never moved it. A payment without a settlement row is not
  allocatable account credit through the payment-allocation owner; it counts as
  funding and as ledger account credit.
- Audit and one domain event are staged in the same transaction.

## 1. Preview (read-only)

```bash
poetry run python -m scripts.billing.return_legacy_over_allocation preview \
  --allocation-id <allocation> --amount <over-allocation> \
  --invoice-total <invoice total> --remaining-settlement <invoice total>
```

Exit `0` means actionable; `2` means blockers remain. Check `blockers`,
`invoice.stays_paid`, `remaining_allocations`, and `ledger`. Record the
`fingerprint`.

## 2. Confirm

```bash
poetry run python -m scripts.billing.return_legacy_over_allocation confirm \
  <same arguments> --fingerprint <sha256> \
  --reason "<Finance determination and documents relied on>" \
  --evidence-ref <finance-ticket-or-document-ref> \
  --evidence-sha256 <sha256 of the evidence file> \
  --actor <system-user-uuid> --idempotency-key <unique-key>
```

The command rechecks under the account, invoice, payment, and allocation locks and
requires the identical fingerprint (`stale_preview` otherwise). Re-running the
same key returns the stored outcome (`replayed: true`). Exit code `3` means the
owner refused; the JSON `error` names why.

## 3. Verify

Re-run the preview: it must report `allocation_inactive`. The invoice must still
be `paid` with the same total. Confirm the `audit_events` row and the event.

## Never

- Never deactivate or delete an allocation with SQL or the admin shell.
- Never use this to clear a deficit: an invoice that would no longer be fully
  paid, or an allocation that is not exactly the excess, is refused.
- Never post a ledger reversal for a legacy allocation by hand; the ledger
  already holds the credit.
