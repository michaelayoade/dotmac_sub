# Reviewed existing prepaid draft settlement

Use this runbook only when Finance has approved exact service periods for
existing periodless prepaid drafts and each selected successful Payment is
already present in Sub. The command is read-only unless `--apply` is supplied.

Owner: `financial.prepaid_draft_reconciliation`

Permission: `billing:prepaid_reconciliation:repair`

## Evidence required for each invoice

- invoice, unlinked line, subscription, Payment, and settlement identifiers;
- explicit service start and next-billing dates;
- exact invoice total and expected account credit after settlement;
- canonical payment reference (a linked verified proof reference is preferred);
- active Finance approver system-user identifier and approval timestamp;
- ticket reference and lowercase SHA-256 evidence digest; and
- operator actor, reason, and stable idempotency key.

When the correction also replaces an incorrectly paid future period, record
the exact superseded invoice and its verified payment-proof Payment. Do not use
this option for a partial allocation, multiple allocations or entitlements,
refund/reversal evidence, overlapping periods, or an anchor that is not exactly
the superseded entitlement end.

Do not apply when the preview is not `exact_reviewed_draft`. Resolve changed
contract/tax terms, financial activity, refund/reversal evidence, payment
capacity, overlapping coverage, or cutoff-balance differences first.
When the reviewed service period is current, preview must also resolve verified
prepaid funding. `manual_review` with `verified prepaid funding prerequisite is
missing` means the account opening must be repaired through its owning runbook
before settlement; do not attempt apply to discover the same failure.

## Preview

```bash
python -m scripts.billing.settle_reviewed_prepaid_draft \
  --invoice-id <invoice-uuid> \
  --subscription-id <subscription-uuid> \
  --payment-id <payment-uuid> \
  --service-start-on 2026-07-27 \
  --next-billing-on 2026-08-27 \
  --expected-total 37625.00 \
  --expected-remaining-credit 37625.00 \
  --payment-reference <canonical-reference> \
  --approver-system-user-id <approver-uuid> \
  --approver-name "<canonical approver name>" \
  --approved-at <ISO-8601-timestamp-with-offset> \
  --ticket-reference 28519 \
  --evidence-sha256 <64-lowercase-hex-digest>
```

For the reviewed supersession shape, add both identifiers:

```bash
  --superseded-invoice-id <wrong-paid-invoice-uuid> \
  --superseded-payment-id <wrong-payment-proof-payment-uuid>
```

When Finance has approved the released payment-proof credit for the immediately
following continuous period, also add:

```bash
  --fund-next-continuous-period
```

In that mode, `--expected-remaining-credit` is the final balance after the
historical draft and the continuous successor period are both funded. Preview
must show the released proof Payment, successor dates, exact canonical amount,
and currency before apply.

Record the returned fingerprint. Without continuous funding, repeat the preview
separately for each later historical invoice, using the expected credit that
remains after the earlier settlement.

## Apply

Use the exact preview arguments plus:

```bash
  --apply \
  --fingerprint <preview-fingerprint> \
  --idempotency-key <stable-ticket-and-invoice-key> \
  --actor <operator-identity> \
  --actor-system-user-id <operator-system-user-uuid> \
  --reason "Finance-approved historical prepaid draft settlement"
```

The owner rejects a stale fingerprint and replays the same committed result for
the same idempotency key. Never replace this command with an invoice-status or
subscription-anchor update.

## Verify after every apply

Confirm all of the following before proceeding to the next invoice:

1. The invoice is `paid`, its balance is exactly zero, and its allocation uses
   the selected Payment for the exact invoice total.
2. One active entitlement links the invoice and selected subscription for the
   approved half-open interval `[service_start, next_billing)`.
3. `Subscription.next_billing_at` equals the approved period end.
4. The account credit equals the reviewed expected remaining credit.
5. No second allocation, entitlement, invoice debit, credit-note application,
   refund, or reversal was created.
6. Invoice metadata and audit evidence contain the approver, timestamp, ticket,
   payment reference, evidence digest, and preview fingerprint.
7. Access is active only when current canonical coverage permits restoration;
   an expired historical period alone must not restore service.
8. When supersession was selected without continuous funding, the old invoice
   is `void`, its allocation is inactive, its entitlement is `reversed`, its
   released payment remains customer credit, and no unrelated allocation or
   entitlement changed.
9. When continuous funding was selected, the released payment alone funds one
   new paid invoice for the immediately following period, its exact allocation
   and entitlement are active, the anchor equals that entitlement end, and the
   final credit equals the reviewed expectation.
10. Financial restoration never clears an unrelated administrative lock;
    resolve that lock separately through its own owner and evidence.

For a sequence of historical invoices, apply them chronologically and rerun a
fresh preview before each write. At the final cutoff, verify the subscription
expiry and next billing date against the last entitlement boundary.
