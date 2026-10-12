# Reviewed paid prepaid period replacement

> **Policy (2026-10-12): billing repair writes are disabled.** `--apply` on
> this command is refused (non-zero exit, nothing is written). The dry-run
> preview still works. Record the case for the billing exceptions review
> instead, pending the ADR 0007 obligation-identity migration. Any `--apply`
> steps below are retained for history only.

Use this runbook only when one fully paid prepaid invoice covers the wrong
future-shifted period, the immediately preceding period is missing, and the
same Payment must fund the corrected period without changing customer credit.
It is dry-run first and requires `billing:prepaid_reconciliation:repair` on the
named operator principal.

Do not use this command when a periodless draft already exists, the Payment is
refunded or reversed, more than one allocation or entitlement exists, coverage
is not continuous, another document overlaps the replacement period, or the
contract charge differs. Those cases remain manual review under their owning
workflow.

## Preview

Use exact timezone-aware instants. Do not convert a documented UTC anniversary
to Lagos midnight.

```bash
poetry run python -m scripts.billing.replace_reviewed_paid_prepaid_period \
  --subscription-id <subscription-uuid> \
  --superseded-invoice-id <wrong-paid-invoice-uuid> \
  --payment-id <sole-funding-payment-uuid> \
  --service-period-start <ISO-8601-instant> \
  --service-period-end <ISO-8601-instant> \
  --expected-total <exact-gross-total> \
  --expected-remaining-credit <unchanged-credit> \
  --payment-reference <verified-proof-reference> \
  --approver-system-user-id <finance-approver-uuid> \
  --approver-name "<canonical active approver name>" \
  --approved-at <ISO-8601-instant> \
  --ticket-reference <ticket> \
  --evidence-sha256 <64-character-digest>
```

Proceed only when `disposition` is `exact_paid_period_replacement` and
`actionable` is true. Finance must verify the account, subscription, invoice,
allocation, entitlement, Payment, old and new interval bounds, total, current
credit, released payment capacity, proof reference, void fingerprint, and
preview fingerprint against the evidence package.

## Apply

Deployment does not authorize a customer repair. Apply only after the change is
released and the named production operator has separate authorization for the
exact preview fingerprint and production host.

```bash
poetry run python -m scripts.billing.replace_reviewed_paid_prepaid_period \
  <the same preview arguments> \
  --apply \
  --fingerprint <reviewed-preview-fingerprint> \
  --idempotency-key <stable-ticket-and-invoice-key> \
  --actor <operator-identity> \
  --actor-system-user-id <operator-system-user-uuid> \
  --reason "Finance-approved paid prepaid period replacement"
```

The owner locks and re-previews every named fact. A stale fingerprint,
permission failure, payment-capacity change, overlap, contract drift, or
participant mismatch rolls back the entire transaction.

## Verify

Confirm all of the following before closing the ticket:

- the superseded invoice is void, its allocation inactive, and its entitlement
  reversed;
- the replacement invoice is paid and has the exact reviewed service bounds;
- the same Payment is allocated once to the replacement invoice;
- the replacement entitlement matches the invoice bounds;
- `next_billing_at` equals the replacement period end;
- customer credit equals the reviewed pre-repair value; and
- the audit record, `prepaid_service.renewed` event, metadata fingerprint, and
  idempotency evidence exist.

Stop on any mismatch. Do not retry with a different fingerprint or make direct
database edits.
