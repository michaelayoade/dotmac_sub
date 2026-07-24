# Prepaid draft invoice reconciliation

Status: cut over for new funding events; historical application remains
dry-run-first and operator reviewed.

Owner: `financial.prepaid_draft_reconciliation`

## Problem

A prepaid billing period could be represented by two competing write paths:

1. monthly billing created a draft invoice and projected a future billing
   anchor; and
2. a later account-credit event skipped drafts, posted a direct renewal debit,
   created entitlement, and advanced the anchor without closing the draft.

The same visible symptom also occurs when a draft is almost funded. For example,
an NGN 18,812.50 invoice with only NGN 18,812.00 of exact payment-backed credit
is short NGN 0.50. That is insufficient funding, not a rounding condition.

## Canonical policy

- `financial.invoices` owns invoice lifecycle and document state.
- `financial.account_credit_applications` owns the exact payment-backed credit
  projection and payment allocation.
- `financial.prepaid_service_renewals` owns direct renewal debit and entitlement
  evidence when no authoritative draft exists.
- `financial.prepaid_draft_reconciliation` is the only classifier and repair
  coordinator when a prepaid draft already exists.

An existing prepaid draft has first claim on the service-period document
boundary. A funding-change consequence checks it before an invoice-less direct
renewal:

- exact native payment-backed funding equal to or above the full balance:
  issue and fully settle the draft atomically;
- any shortfall, including NGN 0.50: keep the draft unchanged and do not create
  entitlement;
- visible but legacy/unbacked credit: keep the draft unchanged for evidence
  reconstruction;
- one exact direct-renewal debit and entitlement overlapping the draft: close
  the duplicate draft through the invoice owner with zero economic delta;
- multiple drafts, mixed lines, partial activity, or ambiguous coverage:
  require manual review.

No path rounds a shortfall, invents a payment, marks an underfunded invoice
paid, or creates a second entitlement.

## Preview and confirmation

`scripts/billing/reconcile_prepaid_drafts.py` is read-only by default. It reports
the disposition, recommended action, exact payment-backed credit, unbacked
credit, shortfall, evidence identifiers, and a SHA-256 evidence fingerprint.
Invoke it from the repository root as a module:

```bash
poetry run python -m scripts.billing.reconcile_prepaid_drafts
poetry run python -m scripts.billing.reconcile_prepaid_drafts \
  --invoice-id INVOICE_UUID
```

Apply is limited to one reviewed invoice and requires:

- the exact preview fingerprint;
- an effective timestamp;
- a stable idempotency key;
- an actor and reason.

```bash
poetry run python -m scripts.billing.reconcile_prepaid_drafts \
  --apply \
  --invoice-id INVOICE_UUID \
  --fingerprint REVIEWED_SHA256 \
  --effective-at 2026-07-23T12:00:00Z \
  --idempotency-key prepaid-draft-INVOICE_UUID-v1 \
  --actor operator@example.com \
  --reason "Reviewed exact funding evidence"
```

The owner locks account first and invoice second, recomputes the preview, and
fails closed if any source fact changed. The invoice transition, payment
allocations or zero-delta closure, audit evidence, metadata, and idempotency
reservation commit together.

## Rollout

1. Deploy the funding-change draft-first guard.
2. Run the full dry-run cohort and retain the reviewed JSON.
3. Apply exact payment-backed cases in small batches, one invoice per command.
4. Apply exact direct-renewal overlap closures separately.
5. Reconstruct legacy/unbacked funding only through its evidence owner; then
   re-preview.
6. Leave insufficient and ambiguous cases unchanged.
7. Verify paid invoice/zero balance/entitlement/anchor for settlements, or void
   invoice/existing entitlement/no new debit for overlap closures.

This change does not mutate historical customer records during deployment.
Backlog state changes occur only through an explicit reviewed apply command.
