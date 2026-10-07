# Prepaid period purchase and outage compensation

## Decisions

- A customer may buy 1–12 monthly service periods for one prepaid subscription.
- The quote starts at the current funded coverage tail. Each calendar period is
  priced and VAT-rounded independently; the checkout total is the sum of those
  immutable period totals.
- Any open invoice balance blocks a future-period purchase. An open confirmed
  customer outage also blocks new checkout initiation.
- Provider confirmation records the actual captured amount and reserves the
  receipt for its purchase before attempting service settlement. The invoice,
  allocation, entitlement, anchor, and intent-completion consequence uses the
  owner savepoint. A rejected consequence leaves no partial service documents;
  confirmed money and a `review_required` purchase remain durable together.
  Invoice construction, issuance, allocation, and payment finalization expose
  typed domain errors to this owner. Legacy HTTP validation is translated at
  the billing participant boundary before the purchase savepoint handles it.
  Infrastructure/database errors roll back the command for whole-command retry.
  The selected payment may fund only its purchase invoices.
- Cancellation is scheduled at the final paid-through boundary. Purchased
  periods are not prorated or automatically refunded, and plan changes wait for
  that boundary.
- Base subscription price is the only purchasable recurring charge. The owner
  fails closed if an active recurring add-on, usage allowance, explicit service
  end, or unresolved lifecycle/plan change is present. Initial support is NGN,
  monthly, unmetered base subscriptions. Other products require their own
  allowance and currency acceptance before this feature is enabled for them.
- One live quote or payment intent may exist per subscription. The account and
  subscription locks and partial unique index enforce this across different keys.
  Any held review blocks new checkout creation under the same locks. Historical
  receipt reviews are excluded from the unique index so a late capture on an old
  purchase cannot erase cash when a newer checkout already exists. A never-started
  expired quote can be retired; an intent with uncertain capture remains unresolved
  until provider review.
- A saved-card or hosted checkout replay returns the existing payment reference.
  A transport timeout cannot authorize a second charge. Browser retries retain
  the same checkout key. Verification, webhook, and reconciliation all use the
  purchase settlement owner, including wrong-amount receipts.
- Full tax facts, customer tax-policy version, period boundaries, and per-period
  totals are fingerprinted and persisted. Purchase `created_at` is the exact
  quote evaluation time. Settlement revalidates current authoritative evidence
  under the account/subscription locks at that original time, then issues lines
  with the frozen tax snapshot. Changed evidence holds the receipt for review.
- A delayed provider confirmation uses an explicit verified capture timestamp.
  Missing capture evidence after quote expiry requires review. A provider's
  transaction creation timestamp is not accepted as proof of capture time.

## Ownership

`financial.prepaid_period_purchases` is the sole writer of purchase headers,
period rows, purchase invoices, their selected-payment allocations, resulting
entitlements, and the final subscription billing anchor. Gateway verification
records confirmed cash, then delegates the settlement consequence to that owner
through the authorized savepoint. Generic funding, renewal, invoice allocation,
and historical allocation repair exclude reserved purchase receipts. Financial
position displays the money as held rather than generally spendable prepaid
funding; cash and ledger history remain intact.

`financial.outage_compensation` is the sole writer of outage compensation
decisions and zero-value compensation entitlements. It consumes only finalized
`CustomerOutageInterval` evidence. It does not change subscription status and
does not rewrite purchased invoices, VAT, or service periods.

`financial.purchased_service_coverage` reads exact purchase coverage facts for
lifecycle and catalog admission. `financial.purchase_payment_recovery_state`
owns purchase state transitions following confirmed refunds/reversals or verified unsuccessful unpaid checkout observations as a
flush-only payment transaction participant. These lower-level boundaries read
persisted facts and never call the purchase settlement coordinator; the owner
dependency graph remains acyclic.

## Outage policy

Posting remains manual, as selected by Michael on 7 October 2026 and required by
`OUTAGE_SLA_SPINE.md`. The flag permits proposal collection and approved posting;
it never approves an individual remedy. The six-hour control is an internal
proposal threshold, not an invented contractual SLA.


- The default minimum eligible outage is six hours and is editable as
  `billing.outage_compensation_min_hours`.
- Durations are calculated in exact seconds. All connected finalized eligible
  intervals are unioned, including previously consumed evidence. Previously
  credited clock ranges are subtracted before awarding additional time. A
  later overlapping finalization cannot award the same second twice, and
  connected below-threshold history can qualify when new evidence arrives.
- Compensation is capped to the intersection of outage time and already funded
  entitlement coverage for the affected subscription.
- A qualifying event records an `awaiting_approval` proposal without an
  entitlement, anchor change or schedule rebase. A separate approved decision
  appends one zero-value entitlement to the then-current funded tail. Each interval receives only one consumption link; historical
  evidence can inform a later delta without another link or another full award.
- Approved grants require active prepaid service, exact evidence, current
  funded coverage, and a funded tail later than processing time. Suspended,
  terminated, postpaid, expired, ambiguous, or conflicting service changes
  produce durable review decisions. Historical grants lacking credited ranges
  require reviewed backfill before staff can approve another delta award.
- Resolved/discarded outage events trigger the compensation owner after the
  downtime ledger commits. Per-subscription owner-output receipts make event
  replay an exact no-op; no whole-customer financial sweep is introduced.
- Pending `next_cycle` cancellation/expiry schedules that target the old funded
  tail are rebased atomically to the compensated tail only when their reviewed
  lifecycle head still matches the previous head. Stale intent cannot be revived.
  Explicit dates that would truncate compensation route the decision to review.
- The customer service page projects an active network interruption without
  mutating subscription lifecycle status. Checkout remains blocked by the
  open outage interval until recovery is finalized.
- Planned-maintenance exclusions and ambiguous evidence are recorded as
  explicit decisions; they are never silently discarded.
- Decisions retain their funded-entitlement dependencies. Confirmed refunds or
  reversals retract dependent outage grants through the entitlement owner before
  anchor rebuild. The original award record remains immutable accounting history.

## Reviewed recovery

`scripts/billing/recover_period_purchase.py` previews one purchase or one unresolved
outage review without writes. Apply requires the exact current preview fingerprint,
an idempotency key, an active staff principal holding
`billing:prepaid_reconciliation:repair`, and an audit reason. No recovery command
charges a card or guesses provider payment evidence.

```
python -m scripts.billing.recover_period_purchase --purchase-id <uuid>
python -m scripts.billing.recover_period_purchase --purchase-id <uuid> --apply --fingerprint <reviewed-fingerprint> --idempotency-key <key> --actor-system-user-id <staff-uuid> --reason "Reviewed receipt retry"
python -m scripts.billing.recover_period_purchase --subscription-id <uuid> --review-decision-id <decision-uuid>
```

Purchase recovery reports `await_provider`, `close_unpaid_checkout`,
`start_new_checkout`, `resolve_blocker`,
`refund_or_provider_review`, `retry_settlement`, or `complete`. A retry uses the
existing exact receipt and frozen purchase, and persists its reviewed key for
completed replay. Changed quotes, wrong amounts/currencies, uncertain capture,
partial refunds, and additional captures require provider/finance resolution.
Refunds use the existing payment refund owner; no purchase repair manufactures
refund cash or releases an unconfirmed receipt.

A second genuine capture is recorded separately and held under the purchase
reservation. It cannot replace the original payment or produce duplicate periods.
Refunding that additional receipt does not cancel periods funded by the original.
Lifecycle/plan-change guards include purchased periods and their dependent outage
grants, and require resolution of pending receipts before service changes.

Outage recovery re-evaluates an unresolved decision against current authoritative
evidence and records a new decision. The original review links to its resolution;
consumed intervals are not rewritten. A still-ambiguous review cannot force a grant.

## Approval and shared clock evidence

Finance reviews receipts and proposals at `/admin/billing/service-period-review`.
Each receipt shows its provider reference, currency and collected/refunded/held
amounts. Held money is displayed separately from available credit in customer billing.

`approve_outage_compensation` requires an active system-user principal with
`billing:outage_compensation:approve`, different human maker and approver, current
fingerprint, idempotency key and reason. The owner rechecks live permissions and
all financial/clock facts. Service principals cannot approve. The original
proposal and approved decision are linked; approval identity and reason persist.

`financial.compensated_service_time` owns append-only original clock-range claims.
Pause resume, new bulk service extensions and approved outages stage claims in
their account-locked grant transaction. Its history resolver reads facts without
calling producer coordinators. Exact historical pause grants and outage snapshots
are deducted. Rounded legacy extensions without exact per-service clock mappings
require staff attestation, even after a later renewal. Existing service is not
clawed back. History remains present after refunds and reversals; withdrawing a
grant never automatically authorizes another award.

Legacy review uses the repair permission and source-fingerprint checks:

```
python -m scripts.billing.recover_period_purchase --legacy-extension-entry <entry-uuid>
python -m scripts.billing.recover_period_purchase --legacy-extension-entry <entry-uuid> --credited-from <ISO-with-offset> --credited-until <ISO-with-offset> --apply --fingerprint <reviewed-fingerprint> --idempotency-key <key> --actor-system-user-id <staff-uuid> --reason "Verified original credited clock"
python -m scripts.billing.recover_period_purchase --subscription-id <uuid> --review-decision-id <proposal-uuid> --approve-outage --apply --fingerprint <reviewed-fingerprint> --idempotency-key <key> --actor-system-user-id <approver-uuid> --reason "Reviewed funded outage remedy"
```

Confirmed failed/abandoned intents with no receipt or settled service release
the purchase restriction through the payment-recovery record participant.
Timeout, not-found and expired-but-unverified outcomes do not. Old references
remain; a genuine late capture is held even when a newer checkout exists.
Browser keys change only when the owner explicitly confirms a fresh checkout is safe.

The accrual owner's typed purchase-admission query preserves the provisional
recovery hold; `ended_at` alone is not finalization. Quote dates, VAT and expiry
are visible before payment. Both fetch requests carry the live CSRF token;
stale previews cannot authorize payment and unknown retries retain the same key.

## Rollout

Revision `647_purchase_outage_approval` merges both current native histories and
adds approval provenance, immutable clock claims and the additive permission.
Downgrade refuses to erase proposal, approval or claim evidence. There is no
stamping, metadata schema substitution or deletion of historical data.


Both `billing.prepaid_period_purchase_enabled` and
`billing.outage_compensation_enabled` default to false. Schema deployment and
historical overlap/reconciliation checks precede enabling either writer.

Migration filenames 644 and 645 remove numeric collisions with current main;
their original `636_service_period_purchase_contract` and
`637_prepaid_period_purchase_intent_contract` revision identities are preserved
for databases that may already have applied the PR. Revision
`646_prepaid_purchase_safety` merges the purchase history with main's 642 head,
adds receipt reservations, capture timestamps, review resolution links, and the
single-live-purchase constraint. Duplicate existing live purchases abort the
migration for reviewed resolution instead of discarding potentially captured cash.

Before enabling either flag, run fresh-schema and predecessor-to-head Alembic
rehearsals, the focused billing/outage tests, and the independent PostgreSQL
checkout/settlement concurrency tests on a disposable migrated database. Run the
full prescribed CI suite and browser checkout acceptance. Local source/math
checks cannot substitute for these gates. Staging acceptance remains part of the
existing release process; lack of staging access does not justify enabling this
feature or experimenting on the operational local database.
