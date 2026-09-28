# Reviewed prepaid invoice sequence reconstruction

Use this procedure only for a Finance-approved historical sequence whose
payments, opening position, invoice periods, and allocation split must be
reconstructed together. It is dry-run first and requires
`billing:prepaid_reconciliation:repair` on the named operator principal.

Do not use it for a current or future period, a single ordinary draft, an
estimated payment split, an unreviewed opening balance, or a request to restore
service. Resolve refunds, reversals, competing invoices, overlapping coverage,
contract/tax drift, or ambiguous ledger evidence through their owning workflow.

## Prepare the evidence manifest

Create an operator-local JSON file; do not commit customer manifests. Use this
shape, preserving chronological document and allocation order:

```json
{
  "subscription_id": "<uuid>",
  "documents": [
    {
      "invoice_id": "<uuid>",
      "line_id": "<uuid>",
      "service_start_on": "2026-06-29",
      "next_billing_on": "2026-07-29",
      "expected_total": "18812.50"
    }
  ],
  "allocations": [
    {
      "payment_id": "<uuid>",
      "invoice_id": "<uuid>",
      "amount": "16812.00"
    }
  ],
  "settlement_evidence": [
    {
      "payment_id": "<uuid>",
      "unallocated_ledger_entry_id": "<uuid>"
    }
  ],
  "existing_allocation_evidence": [
    {
      "allocation_id": "<uuid>",
      "invoice_ledger_entry_id": "<uuid>",
      "balancing_ledger_entry_id": "<uuid>"
    }
  ],
  "expected_opening_credit": "14811.50",
  "expected_post_repair_credit": "0.00",
  "expected_authoritative_prepaid_funding": "0.00",
  "approval": {
    "approver_system_user_id": "<uuid>",
    "approver_name": "<canonical active Finance approver name>",
    "approved_at": "<ISO-8601 timestamp with offset>",
    "ticket_reference": "<ticket>",
    "evidence_sha256": "<64 lowercase hex characters>"
  }
}
```

Every selected payment must be fully distributed by `allocations`. Every target
invoice must be exactly settled by its selected allocations plus the explicitly
listed pre-existing allocations. Include one settlement ledger selection for
every selected payment, including payments whose settlement row already exists.

## Preview

```bash
poetry run python -m scripts.billing.reconstruct_reviewed_prepaid_invoice_sequence \
  --manifest /secure/operator/path/reviewed-sequence.json
```

Proceed only when `disposition` is `exact_sequence` and `actionable` is true.
Finance must verify the returned invoice/payment identifiers, service bounds,
opening credit, post-boundary credit, authoritative prepaid funding, totals,
and fingerprint against the evidence package.

## Apply

Deployment of the implementation does not authorize a data repair. Apply only
after the production change is released and the named production operator has
separate authorization for the exact reviewed fingerprint.

```bash
poetry run python -m scripts.billing.reconstruct_reviewed_prepaid_invoice_sequence \
  --manifest /secure/operator/path/reviewed-sequence.json \
  --apply \
  --fingerprint <reviewed-preview-fingerprint> \
  --idempotency-key <stable-ticket-and-sequence-key> \
  --actor <operator-identity> \
  --actor-system-user-id <operator-system-user-uuid> \
  --reason "Finance-approved historical prepaid invoice sequence reconstruction"
```

The owner locks and re-previews all evidence. A stale fingerprint, permission
failure, or participant mismatch rolls back every document and allocation.

## Verify

After an authorized apply, confirm:

1. Every target invoice is `paid` with zero balance and the exact approved
   payment allocations.
2. Every new allocation has invoice and consumption ledger links; those new
   rows have zero customer-position effect.
3. Missing settlement rows were attached only to the selected existing ledger
   credits; no cash or replacement credit was posted.
4. One active entitlement covers each approved half-open invoice interval, with
   no gap or overlap.
5. `Subscription.next_billing_at` equals the final entitlement end, while an
   expired final period leaves access unchanged.
6. Post-boundary reusable credit and authoritative prepaid funding equal the
   manifest expectations.
7. The customer financial position delta is exactly zero.
8. Each invoice carries the same ticket, Finance approval, evidence digest,
   fingerprint, command id, and idempotency metadata.
9. One sequence audit record and one
   `prepaid_invoice_sequence.reconstructed` durable event exist.
10. Repeating the same command returns `replayed: true` and creates no rows.

Stop and escalate to Finance if any value differs. Never compensate with direct
invoice status, allocation, ledger, entitlement, anchor, or access updates.
