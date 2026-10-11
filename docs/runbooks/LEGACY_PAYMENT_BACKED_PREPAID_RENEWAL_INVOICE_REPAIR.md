# Legacy payment-backed prepaid renewal invoice repair

Owner: `financial.prepaid_service_renewals`

Use this runbook only for one reviewed historical prepaid period with an
active, payment-linked customer-position debit, an active entitlement sourced
from that debit, and no invoice or `AccountAdjustment`. Deploying the code does
not repair data. Preview and apply are separate one-entity operations.

Never include customer names, contact details, payment narration, or private
approval documents in arguments, logs, fixtures, or source control. Retain only
internal UUIDs and non-secret evidence references.

## 1. Preview (zero writes)

```bash
poetry run python -m scripts.billing.repair_legacy_payment_backed_renewal_invoice \
  --account-id <account-uuid> \
  --subscription-id <subscription-uuid> \
  --payment-id <payment-uuid> \
  --ledger-entry-id <ledger-entry-uuid> \
  --entitlement-id <entitlement-uuid> \
  --expected-period-start <ISO-8601-with-offset> \
  --expected-period-end <ISO-8601-with-offset> \
  --expected-gross-total <decimal> \
  --expected-currency <ISO-4217-code> \
  --expected-available-funding-after-repair <decimal>
```

Continue only when `disposition=exact_legacy_chain`, `actionable=true`, every
identity and expected value matches the reviewed evidence, funding before and
after are equal, and the fingerprint is present. Stop on `manual_review`, any
mismatch, or any error. Do not edit data to make a preview pass.

## 2. Apply the exact preview

```bash
poetry run python -m scripts.billing.repair_legacy_payment_backed_renewal_invoice \
  --apply \
  --account-id <account-uuid> \
  --subscription-id <subscription-uuid> \
  --payment-id <payment-uuid> \
  --ledger-entry-id <ledger-entry-uuid> \
  --entitlement-id <entitlement-uuid> \
  --expected-period-start <ISO-8601-with-offset> \
  --expected-period-end <ISO-8601-with-offset> \
  --expected-gross-total <decimal> \
  --expected-currency <ISO-4217-code> \
  --expected-available-funding-after-repair <decimal> \
  --preview-fingerprint <sha256> \
  --actor-user-id <operator-user-uuid> \
  --permission-evidence <non-secret-approval-reference> \
  --reason-evidence-reference <non-secret-finance-case-reference> \
  --idempotency-key <stable-16-to-120-character-key>
```

The adapter verifies the active user and `billing:ledger:write` permission,
then releases its read transaction before entering the owner command. Changed
payment, settlement, ledger, entitlement, tax, period, funding, overlap, or
anchor evidence rolls the whole operation back.

## Acceptance

- the returned invoice is active, paid, non-pro-forma, and has zero balance;
- one active allocation links only the selected Payment for the gross total;
- payment application history reports that gross once as invoice-applied and
  no longer also reports it as direct prepaid consumption;
- one structural credit reverses the named legacy debit;
- the old entitlement is reversed and one invoice-line-backed replacement has
  the identical half-open service interval;
- available funding and `next_billing_at` equal their previewed values;
- the customer portal invoice-history query returns the invoice;
- one immutable audit row and one
  `prepaid_service.renewal_document_corrected` event identify the repair; and
- replay returns the same identifiers without additional financial, coverage,
  audit, or event evidence.
