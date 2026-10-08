# Prepaid renewal terms — finance review

Owner: `financial.prepaid_renewal_terms_backfill` (ADR 0007 stage 3,
transitional). Work-item and alert owner label: `financial-billing`.

Prepaid enforcement fails closed with `renewal_terms_unresolved` when a
collectible prepaid subscription has no positive contracted amount
(`Subscription.unit_price` NULL or `<= 0`) or lacks the charge inputs the
renewal resolver needs (an active recurring price row for currency/cadence
metadata and a proven monthly cadence). The account is then neither
suspended nor restored on money grounds until finance resolves it.

Every such subscription carries one admin work item with fingerprint
`prepaid-renewal-terms:evidence:<subscription_id>`. Its `details` name the
`decision`, `insufficiency_reasons`, `distinct_paid_amounts`, `next_action`,
`sla_due_at` (72 hours from opening) and this runbook. Alerts
`PrepaidRenewalTermsUnresolved` and `PrepaidWorkItemsOverdue`
(`deploy/observability/prepaid_enforcement.rules.yml`) link here.

## What is never allowed

- Never infer the amount from the catalog. The current catalog price is not
  the customer's contract (ADR 0007 Phase 1;
  `docs/FINANCIAL_ACCESS_ENFORCEMENT.md`, "Exact prepaid renewal charge").
- Never write `0.00` to make an item go away. Zero is a claim, not an absent
  price, and enforcement treats it exactly like NULL. Free or discounted
  service for one customer is a billing treatment.
- Never use direct SQL, a Python shell, or the generic admin subscription
  edit form to set `unit_price`. The generic edit now refuses an explicit
  `unit_price` on a collectible prepaid subscription without a contracted
  amount (HTTP 409) and points here.
- Never add a catalog price row to an offer that exists to describe
  per-customer free service ("... - Non Billing", staff offers). That turns
  a concession into billed service for everyone on the offer.
- Never approve your own request. The record command refuses it.
- Never decide from an offer's name: work from the structured decision and
  reasons on the work item.

## Prioritise

1. Items past `sla_due_at` (the `PrepaidWorkItemsOverdue` alert).
2. Subscriptions that are `suspended` or already due for renewal: their
   accounts cannot be restored after payment until the terms are resolved.
3. Active subscriptions by oldest `sla_due_at`.

List the current cohort and decisions (read-only):

```bash
poetry run python -m scripts.billing.billing_target_shadow preview-renewal-terms
```

## Work each decision

### `no_evidence` — reviewed renewal-term record

No paid base-subscription invoice line exists, so the amount must come from
an external record: the signed order form, the approved quote, the migrated
billing system's contract, or a finance-approved price letter.

1. **Requester** (staff member A, holding `billing:renewal_terms:record`):
   save the evidence document, take its SHA-256
   (`shasum -a 256 <file>`), and store it where finance keeps review evidence.
   Read the stored price from the work item or subscription: `none` when NULL,
   else the value (for example `0.00`).

   ```bash
   poetry run python -m scripts.billing.billing_target_shadow \
     request-renewal-term-record \
     --subscription <subscription_id> \
     --amount 17500.00 \
     --expected-current-amount none \
     --reason "Order form FIN-2026-1008 signed 2026-02-01: 17,500/month ex VAT" \
     --evidence-ref "finance-evidence/FIN-2026-1008/order-form.pdf" \
     --evidence-sha256 <64-hex sha256> \
     --actor <A's SystemUser id> \
     --idempotency-key renewal-term-request:<subscription_id>:FIN-2026-1008
   ```

   The output's `request_id` goes to the approver. Nothing changes yet:
   `price_changed` is `false` and the work item stays open.

2. **Approver** (staff member B, a different person with the same
   permission): open the evidence, check its SHA-256 matches, check the amount
   is the exclusive monthly contract amount, then approve by restating it:

   ```bash
   poetry run python -m scripts.billing.billing_target_shadow \
     list-renewal-term-record-requests --subscription <subscription_id>

   poetry run python -m scripts.billing.billing_target_shadow \
     approve-renewal-term-record \
     --request <request_id> \
     --amount 17500.00 \
     --approver <B's SystemUser id> \
     --idempotency-key renewal-term-approve:<request_id>
   ```

   In one transaction the approval re-checks every precondition under the
   subscription lock, sets `unit_price`, emits
   `prepaid_renewal_terms.recorded` (both staff ids, reason, evidence ref and
   SHA-256, previous and new amount), and resolves the work item
   (`work_item_resolved: true`). The next prepaid sweep computes the account's
   threshold and takes the action it was owed.

Re-running either command with the same idempotency key replays. Use a new
key for a new proposal. If the subscription's amount changed after the
request, the approval refuses with `stale_current_amount`: submit a new
request against the current value.

### `ambiguous_amounts` / `insufficient_cycle_evidence` — reviewed record

The paid evidence exists but cannot prove one amount (several distinct paid
amounts, or no line with canonical full-cycle proof — prorated, partial
period, quantity or currency mismatch). Finance decides which amount is the
contract and records it with the same request/approve flow. Cite the
deciding document in `--reason` and the evidence (it may be a reconciliation
worksheet listing the invoices). The backfill never picks between conflicting
amounts itself.

### `missing_charge_inputs` — a price alone cannot clear it

The record command refuses these (`charge_inputs_missing`), because a price
without currency/cadence metadata still yields no renewal charge. Work by
reason:

- `no_active_recurring_price` on a "... - Non Billing" or staff offer: these
  are legacy per-customer concessions, not free-to-everyone products
  (`docs/designs/SUBSCRIPTION_BILLING_TREATMENTS.md`). Move each
  subscription to the real offer at the customer's real contracted value
  through the plan-change flow (never by editing `unit_price`), then approve
  a complimentary arrangement with `POST
  /api/v1/billing-treatments/subscriptions/{subscription_id}/preview` and
  `POST /api/v1/billing-treatments/subscriptions/{subscription_id}`
  (`billing:treatment:write`): reason, sponsor or cost-centre evidence, and
  a ≤366-day interval aligned to billing boundaries, re-approved annually.
  An effective treatment removes the subscription from this cohort and the
  item resolves on the next sweep. If the customer should in fact pay, move
  them to the real offer and resolve any remaining evidence gap with a
  reviewed record. Do not add a price row to the non-billing offer.
- `no_active_recurring_price` on a genuinely billable offer: the catalog owner
  adds the missing active recurring `OfferPrice`/`OfferVersionPrice` row
  through the governed catalog admin path. That row is currency/cadence
  metadata only — it never becomes this customer's amount. If `unit_price`
  is still missing afterwards the item becomes `no_evidence` (or another
  evidence decision) and follows the record flow above.
- `cadence_unproven`: the subscription and its price row both lack a billing
  cycle. The catalog owner sets the cadence on the price row (or the
  subscription's cadence through its owner).
- `cadence_incompatible:annual` (or any non-monthly cycle): prepaid renewal is
  monthly-only today. This needs a product decision (move the customer to a
  monthly plan through the plan-change flow) or ADR 0007 Phase 2 contract
  cadence support. Leave the item open and escalate; do not relabel the
  cadence to make it pass.
- `charge_currency_mismatch`: the price row's currency differs from the
  enforcement currency. Catalog owner fixes the price row currency.

### `unit_price` is `0.00`

The decision is usually `no_evidence`. Decide what the zero means:

- Genuinely complimentary for this customer → billing treatment (see above).
  A prepaid treatment requires a positive contracted value, so the real
  contracted amount must exist first.
- Not complimentary → reviewed record with `--expected-current-amount 0.00`.

A service on an offer whose catalog explicitly declares one active recurring
price of zero (a product free to every eligible subscriber, classified
`explicit_zero_price` by `financial.customer_chargeability`) is non-billable
for the threshold and is outside this cohort; it never needs a work item.

### `correction_fail_closed` — previously restored amount was reverted

A restored amount was reverted after audit or finance review. Record the
correct amount with the reviewed record flow (request + approve).
Superseding an amount this owner restored from paid evidence uses
`correct-renewal-terms`, which now requires `--actor <SystemUser id>` with
`billing:renewal_terms:record`.

## How items close

- A reviewed record approval resolves the item in the same transaction.
- Every scheduled sweep (`restore_prepaid_renewal_terms`) re-previews the
  cohort; any subscription no longer blocked — priced with intact charge
  inputs, covered by an effective billing treatment, confirmed free by an
  explicit zero catalog price, no longer prepaid, or no longer collectible —
  has its item resolved.
- `missing_charge_inputs` items stay open until the inputs exist; a price
  alone never closes them.

## Permissions

`billing:renewal_terms:record` (migration
`652_renewal_terms_record_permission`) is granted to `admin` and
`finance_manager`. Requester and approver must both hold it and must be
different active staff users. Grant it to another role only deliberately.
