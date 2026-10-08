# Reviewed prepaid invoice sequence reconstruction

Use this procedure only for a Finance-approved historical sequence whose
payments, opening position, invoice periods, and allocation split must be
reconstructed together. It is dry-run first and requires
`billing:prepaid_reconciliation:repair` on the named operator principal.
The reviewed cohort may be one expired invoice when its existing receivable
debit must be reclassified into exact payment and opening-funding settlement
evidence without changing the customer position.

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
  "expected_opening_funding_consumption": "0.00",
  "expected_post_repair_credit": "0.00",
  "expected_authoritative_prepaid_funding": "0.00",
  "approval": {
    "approver_system_user_id": "<uuid>",
    "approver_name": "<canonical active Finance approver name>",
    "ticket_reference": "<ticket>"
  }
}
```

The sequence owner records the named Finance approver and ticket reference as
audit provenance. An approval timestamp or evidence digest may be included when
available, but neither is required by this sequence-specific repair path.

Every selected payment in a multi-document sequence must be fully distributed by
`allocations`. A late-recorded, single-document repair may use only the amount
needed to settle the invoice and retain the previewed residual as reusable
account credit. Every target invoice must be exactly settled by its selected
allocations plus the explicitly listed pre-existing allocations and
opening-funding consumption. Opening funding may be selected only for a single
expired document and must equal the full remaining reviewed opening source.
Include one settlement ledger selection for every selected payment, including
payments whose settlement row already exists. The preview's
`selected_payment_allocation_total`, `selected_payment_residual`, and
`post_boundary_credit` must match the reviewed evidence before apply.
For a provider payment, `selected_payment_total` is the exact settlement-backed
customer credit. It may be lower than the captured payment amount when the
difference is an evidenced gateway fee.

### Select the reviewed calendar basis

Without a `calendar` object, dates retain the existing `business_midnight`
meaning: midnight in Africa/Lagos, persisted as UTC. Never assume that a stored
UTC midnight is Lagos midnight; it is 01:00 in Lagos.

For continuation of a documented historical anniversary, include:

```json
"calendar": {
  "basis": "documented_anniversary",
  "expected_initial_anchor_at": "<exact observed first invoice end, ISO-8601 with offset>"
}
```

This mode preserves the first invoice's exact stored interval and derives later
reviewed dates using its Lagos anniversary clock. The first invoice must already
have the selected subscription line and both period bounds. Its dates must equal
the reviewed Lagos dates and its end must exactly equal both the observed
subscription anchor and the manifest expectation. Missing/partial identity,
different endpoint clocks, changed bounds, or a stale anchor blocks preview.
There is no arbitrary clock-time or timezone override and no automatic fallback.

For example, a documented 00:00 UTC boundary displays as 01:00 Africa/Lagos on
the same date. Continuing that anniversary does not move a preceding paid
invoice or manufacture a one-hour overlap. Actual instant overlaps still block.
Changing an existing paid period to Lagos midnight is a different repair owned
by `financial.prepaid_billing_calendar_reconciliation`, with its own exact paid
invoice/allocation/settlement/entitlement evidence and authorization. Do not
shift that invoice merely to force this sequence to pass.

## Preview

```bash
poetry run python -m scripts.billing.reconstruct_reviewed_prepaid_invoice_sequence \
  --manifest /secure/operator/path/reviewed-sequence.json
```

Proceed only when `disposition` is `exact_sequence` and `actionable` is true.
Finance must verify the returned invoice/payment identifiers, service bounds,
opening credit, post-boundary credit, authoritative prepaid funding, totals,
and fingerprint against the evidence package.
Verify `calendar_basis`, `timezone_name`, `initial_anchor_at`, every returned
`service_periods` UTC and Lagos timestamp, `reviewed_allocation_plan`, and
`expected_post_repair_credit`. Unresolved calendar evidence returns null bounds
and no periods; that is never actionable. A calendar change requires a new
preview and separate authorization of its exact fingerprint.

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
Apply-time typed owner failures are emitted as JSON on stderr with a stable
error code and safe message. For
`financial.prepaid_draft_reconciliation.incomplete_repair`,
the adapter includes only the allowlisted postcondition values:
`remaining_credit`, `expected_remaining_credit`,
`authoritative_prepaid_funding`, `expected_authoritative_prepaid_funding`,
`customer_position_delta`, and `service_period_end`. These values diagnose the
failed invariant; they do not authorize bypassing it. When this owner
postcondition error is reported, the owner transaction has rolled back: stop,
preserve the error JSON, and investigate before any new apply attempt.

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
6. A later billing anchor remains unchanged when the repaired expired period is
   behind already-funded coverage.
7. Post-boundary reusable credit and authoritative prepaid funding equal the
   manifest expectations.
8. The customer financial position delta is exactly zero.
9. Each invoice carries the same ticket, Finance approver, optional approval
   evidence, fingerprint, command id, and idempotency metadata.
10. One sequence audit record and one
   `prepaid_invoice_sequence.reconstructed` durable event exist.
11. Repeating the same command returns `replayed: true` and creates no rows.

Stop and escalate to Finance if any value differs. Never compensate with direct
invoice status, allocation, ledger, entitlement, anchor, or access updates.

## Funding displacement correction

Use `scripts.billing.correct_reviewed_prepaid_sequence_funding` only when one
reviewed case has all of these exact facts: three expired contiguous prepaid
documents; one imported three-period Payment with one existing allocation and
no settlement structure; one unconsumed approved opening for the third period;
one later Payment applied to the misdated middle document; and one full-value
non-prepaid receivable that must receive that released Payment. The preview also
requires the duplicate historical invoice to be already void.

The operator-local manifest names exact UUIDs and timestamps. `documents[0]`
must be the correctly identified paid document, `documents[1]` the paid
misdated document with both `expected_current_period_*` fields, and
`documents[2]` the periodless draft. Include the canonical active Finance
approver, ticket, expected source amounts, opening credit, and final reusable
credit.

Preview first:

```bash
poetry run python -m scripts.billing.correct_reviewed_prepaid_sequence_funding \
  --manifest /secure/operator/path/reviewed-sequence-correction.json
```

Apply only the returned `exact_sequence` fingerprint:

```bash
poetry run python -m scripts.billing.correct_reviewed_prepaid_sequence_funding \
  --manifest /secure/operator/path/reviewed-sequence-correction.json \
  --apply \
  --fingerprint <reviewed-preview-fingerprint> \
  --idempotency-key <stable-ticket-and-correction-key> \
  --actor <operator-identity> \
  --actor-system-user-id <operator-system-user-uuid> \
  --reason "Finance-approved prepaid sequence funding correction"
```

After apply, verify the three paid documents and their exact consecutive
entitlements, the retired displaced allocation, the new historical allocation,
the full allocation to the non-prepaid target invoice, the final billing
anchor, zero reusable credit, zero customer-position delta, and
`payment_rows_created: 0`. A repeat must return `replayed: true`.
