# Tax-inclusive prepaid renewal documentary sequence

> **Policy (2026-10-12): billing repair writes are disabled.** `--apply` on
> this command is refused (non-zero exit, nothing is written). The dry-run
> preview still works. Record the case in the Knowledge debt register
> (`dotmac-debt-register`) under the billing exceptions review for Michael;
> do not script a fix. This holds pending the ADR 0007 obligation-identity
> migration. Any `--apply` steps below are retained for history only.

This is a dry-run-first, entity-scoped procedure. Never substitute a staging or
production connection during development validation. Put approved identifiers,
amounts, fingerprints, actor UUIDs, and ticket references only in the operator's
untracked command record.

## 1. Convert the historical gross adjustment

Preview:

```bash
poetry run python -m scripts.billing.correct_tax_inclusive_renewal_document \
  --account-id <uuid> --subscription-id <uuid> \
  --adjustment-id <uuid> --entitlement-id <uuid> --payment-id <uuid> \
  --expected-invoice-total <decimal> --expected-remaining-credit <decimal>
```

Apply only an actionable, independently reviewed fingerprint:

```bash
poetry run python -m scripts.billing.correct_tax_inclusive_renewal_document \
  --account-id <uuid> --subscription-id <uuid> \
  --adjustment-id <uuid> --entitlement-id <uuid> --payment-id <uuid> \
  --expected-invoice-total <decimal> --expected-remaining-credit <decimal> \
  --apply --preview-fingerprint <sha256> --actor-user-id <uuid> \
  --permission-evidence <approval-or-role-evidence> \
  --reason-evidence-reference <approved-ticket-ref> \
  --idempotency-key <16-to-120-character-key>
```

Stop if preview or apply is unresolved. Verify one active, paid,
non-pro-forma invoice, one selected-payment allocation, unchanged residual
credit, the same period and anchor, one replacement entitlement, and unchanged
manual suspension. Record the returned replacement entitlement UUID.

## 2. Create the exact-anniversary next invoice

Use that returned entitlement as the only documentary reference. Preview:

```bash
poetry run python scripts/billing/reconcile_prepaid_drafts.py \
  --repair-missing-paid-invoice --account-id <uuid> \
  --subscription-id <uuid> --payment-id <uuid> \
  --issued-on <YYYY-MM-DD> --due-on <YYYY-MM-DD> \
  --next-billing-on <YYYY-MM-DD> --expected-total <decimal> \
  --expected-remaining-credit <decimal> \
  --calendar-basis documented_anniversary \
  --expected-initial-anchor-at <aware-ISO-8601> \
  --documentary-entitlement-id <replacement-entitlement-uuid>
```

Apply the identical arguments plus:

```bash
  --apply --fingerprint <sha256> --actor <attributable-actor> \
  --reason <approved-ticket-ref> \
  --idempotency-key <bounded-key>
```

Verify the new invoice is paid, only the selected Payment funded it, remaining
credit equals the approved residual, `next_billing_at` equals the exact period
end, and the subscription remains suspended. Retry uses the same inputs,
fingerprint, and idempotency key; changed evidence requires a new preview.
