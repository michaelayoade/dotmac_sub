# Paystack Automatic Payment Posting

This runbook verifies and repairs the automatic path that records Paystack
payments in Sub. It does not make Paystack authoritative for customer balance,
invoice, subscription, or access state.

## Ownership

- Paystack supplies an external transaction observation: reference, provider
  transaction identity, gross amount, provider fee, currency, and outcome.
- `integration.installations` owns the enabled Paystack capability bindings.
- `financial.payment_webhooks` owns signed webhook ingress and dispatches the
  normalized observation to the canonical settlement owners.
- `financial.payment_reconciliation` owns the bounded scheduled fallback and its
  reconcilable-intent backlog, progress, and age projection. Its candidate
  policy reserves bounded capacity for both stale pending customer payments and
  terminal late-success recovery. Unused capacity flows to the other lane, and
  supported providers are interleaved within each lane starting with the least
  recently served provider. The same owner separately owns a singular,
  fingerprint-bound preview and finance-reviewed command for an exact Paystack
  reference outside the automatic maximum-age window. That command never widens
  the scheduled candidate policy or creates a bulk recovery lane.
- `financial.topup_intents` owns lifecycle transitions and the shared
  customer/admin blocker and retry projection, including separate typed gateway
  attempt and observation progress plus the next reconciliation time. Provider
  adapters only normalize observations and never mutate intent state.
- `financial.account_credit_deposits` owns account-credit deposit settlement.
  The requested deposit is the authorized customer credit; the provider gross
  and fee remain explicit settlement facts.
- Canonical financial owners decide invoice allocation, account balance, and
  subsequent access restoration. Neither the webhook route nor the scheduled
  task decides those states independently.

## Verification status semantics

An HTTP `200` from Paystack proves only that the verification request completed.
The adapter classifies the transaction status in the response data:

- `success` is authoritative settlement evidence;
- `failed` and `abandoned` are terminal unsuccessful observations and immediately
  move a pending intent to a non-blocking local terminal state;
- `pending` is awaiting confirmation, while `ongoing`, `processing`, and `queued`
  remain non-terminal processing observations and block a duplicate attempt until
  the intent's bounded expiry;
- an unknown status, missing reference, transport failure, or unavailable provider
  fails closed as confirmation unavailable. It does not mark the intent failed.

The same typed contract applies to every supported gateway adapter. Safe reason
codes and verification times may be persisted; raw responses, exceptions, and
secrets must not be persisted or displayed. When canonical expiry elapses, the
lifecycle owner projects and persists `expired`, which no longer blocks retry.

Failed, abandoned, canceled, and expired labels are not money locks.
Reconciliation continues within its bounded maximum-age policy, and authoritative
late success can complete those states. Provider transaction identity and existing
payment/event idempotency constraints ensure webhook and reconciliation replay
settle exactly once.

## Bounded queue and progress semantics

The scheduled fallback must not use one oldest-first queue for both customer
payments and terminal late-success recovery. It uses these separate lanes:

- pending intents are eligible only after the stale threshold, inside the
  maximum-age window, and when their next reconciliation time is due;
- failed, abandoned, canceled, and expired intents are eligible for late-success
  recovery inside the maximum-age window when their terminal retry is due.

The configured batch size has a minimum of two. When both lanes are due, each
receives reserved capacity so neither lane can starve the other. A lane that
cannot use its reservation yields that capacity to the other lane. Within each
lane, supported payment providers are interleaved before due intents are
selected. The provider with the oldest committed attempt starts each lane, so
starting priority rotates across runs even when a lane has only one reserved
slot and one provider backlog cannot monopolize it.

Every successfully claimed intent commits typed attempt count/time and its next
eligible time before the provider call begins. A candidate that loses a
concurrent atomic claim is selected but not checked and performs no provider
I/O; that transient gap is expected because the winning worker owns the attempt.
A provider timeout, normalization failure, or rejected financial consequence
therefore remains visible as an attempted repair and cannot leave the same
oldest row pinned at the front of every run.
Normalized provider observations update separate observation count/time,
outcome, and safe reason evidence. The existing
`metadata.gateway_verification` value remains compatibility evidence; it is not
the scheduling cursor.

The read-only backlog is an exhaustive partition of supported unresolved
gateway intents at one observation time:

- pending: fresh, due, cooling down, or outside the automatic window;
- terminal late-success recovery: due, cooling down, or outside the automatic
  window.

The projection reports lifecycle-lane counts, oldest due age, and attempt
progress. A successful task heartbeat, `errors=0`, or a full checked batch is
not proof of health when oldest due age is increasing, checked work is not
advancing, or selected-to-checked gaps persist beyond concurrent claim races.

The canonical Sub Paystack ingress URL is:

```text
https://selfcare.dotmac.io/api/v1/payment-events/paystack
```

In the shared-merchant deployment, Paystack delivers to ERP first. ERP verifies
the original signature and relays the unchanged signed body to this Sub URL for
`DMAC-` references. Do not configure a second direct Paystack destination to Sub
for the same merchant flow; repeated provider or relay delivery is safe but is
not the intended topology.

## Ingress traffic policy

Signed payment-provider callbacks never share the generic API sync-pressure
bucket. Their exact code-owned routes use a dedicated per-provider and
per-source-IP limiter before a database session is acquired. The authoritative
bounded policy is stored on the installation's `payments.webhook.v1` capability
binding and managed from the payment-gateway setup page. Redis and process-local
copies are runtime projections; startup re-materializes them from the binding.

Bulk ERP sync limits and offender-IP lists must never be used to tune signed
payment ingress. A limited provider request returns HTTP `429` with
`Retry-After`; the provider or ERP relay must retry the same signed event, and
the integration inbox preserves idempotency.

## Enable delivery

For a direct deployment, set the URL above as the **live** webhook URL in the
Paystack Dashboard/Canvas. For the shared-merchant deployment, keep ERP as the
single Paystack destination and configure ERP's verified relay to the Sub URL
above.
Do not place the Paystack secret in this repository, a ticket, a pull request,
or an operator note. The signing secret remains in the approved secret store
and is resolved by the Paystack integration binding.

Paystack owns delivery configuration. A Sub deployment can expose and verify
the endpoint, but it cannot prove that the live Paystack account is configured
to send events without signed delivery evidence.

## Safe endpoint check

An unsigned probe may be used only to verify routing and the fail-closed
signature boundary:

```bash
curl -i -X POST \
  https://selfcare.dotmac.io/api/v1/payment-events/paystack \
  -H 'content-type: application/json' \
  --data '{}'
```

Expected result: HTTP `400` with `invalid signature`. This must not create a
provider-event receipt, payment, allocation, or customer credit.

Do not use a fabricated signature or replay a captured production payload.

## End-to-end verification

1. Confirm the Paystack webhook and reconcile capabilities are enabled on the
   installed integration.
2. Confirm the scheduled task
   `app.tasks.payment_reconciliation.reconcile_topups` is enabled at its
   expected cadence.
3. Trigger a controlled live Paystack payment with a unique reference.
4. In **Admin → Integrations → Installed**, inspect the Paystack operational
   evidence:
   - a recent signed webhook receipt exists;
   - the reconciliation runner has a recent heartbeat and result;
   - the result has no rejected candidates;
   - checked and durable attempt progress agree; any selected-to-checked gap is
     transient and explained by another worker's successful claim;
   - checked-pending and checked-terminal evidence confirms both due lanes are
     advancing when both have work;
   - oldest due pending and terminal age is not increasing across healthy runs;
   - cooling-down work has a future next reconciliation time;
   - no pending or terminal intents are stranded outside the automatic window.
5. Verify the canonical payment records preserve the provider gross and fee,
   while account credit equals the authorized deposit amount.
6. Verify allocation, balance, and access changes are traceable to their named
   owners and were not written directly by the webhook adapter.

A `partial` reconciliation result is not success. Investigate its rejection
evidence even when the Celery task completed without raising an exception. A
nominally successful saturated run also requires follow-up when due counts or
oldest due age do not decrease across runs.

## Reconcile a stranded payment

Use the scheduled fallback for work still inside its automatic window. The
operator command accepts only an exact Paystack intent whose canonical status is
`failed`, `abandoned`, `canceled`, or `expired` and whose creation time is older
than the automatic maximum-age window. It rejects `pending`, processing, and
every other non-terminal status even when the row is outside that window. Never
change the maximum-age setting merely to make a historical cohort eligible.

Preview is mandatory and read-only. Supply both the canonical intent UUID and
its exact stored Paystack reference:

```bash
poetry run python -m scripts.billing.reconcile_paystack_reference \
  --intent-id <uuid> \
  --reference <exact-ref>
```

The owner resolves the exact intent, enabled version-pinned
`payments.reconcile.v1` binding, and canonical active `PaymentProvider` through
`financial.payment_gateway_finance`; gateway routing or presentment does not own
that finance identity. It then obtains a fresh normalized Paystack observation
and classifies canonical Payment and provider-event replay evidence. Review the
returned `intent_id`, `reference`,
`intent_status`, `disposition`, `actionable`, `fingerprint`, provider and binding
identities, external transaction identity, gross amount, provider fee,
authorized net amount, currency, provider status, reason code, and any existing
Payment identity. The preview must fail closed or remain non-actionable for a
missing or changed intent/reference, an intent still inside the automatic
window, disabled or ambiguous provider configuration, stale or contradictory
local evidence, provider not-found, pending/processing, failed/abandoned,
unavailable/unknown, or incomplete monetary evidence.

Finance must independently confirm the exact account/intent correlation,
provider transaction identity, currency, gross amount, provider fee, authorized
deposit amount, and duplicate absence. Record a non-secret review/change
reference and a specific reason. Do not put credentials, raw provider payloads,
or unnecessary customer identity in either value.

Apply only the same actionable preview. Pass its SHA-256 unchanged and use a
stable idempotency key for this reviewed recovery:

```bash
poetry run python -m scripts.billing.reconcile_paystack_reference \
  --intent-id <uuid> \
  --reference <exact-ref> \
  --apply \
  --fingerprint <sha256> \
  --actor <finance-operator> \
  --reason <review-reason> \
  --review-reference <change-or-evidence-ref> \
  --idempotency-key <stable-key> \
  --confirm APPLY_REVIEWED_PAYSTACK_RECOVERY
```

Apply first returns an existing immutable recovery run when the same idempotency
key and command fingerprint already succeeded. Otherwise it obtains a second
fresh provider observation and local snapshot, releases that read transaction,
then enters one owner transaction. That root locks and recomputes the preview,
refuses a stale fingerprint, and atomically records the exact review provenance,
canonical provider-event/payment/deposit/allocation/top-up consequences and the
immutable recovery run. The result is either `recovered` or an exact `linked`
replay and includes the durable recovery-run, intent and Payment identities. A
changed or non-success provider observation is not money authority: obtain a new
preview and finance decision if later evidence justifies another attempt.

This command is a current-period posting path, not a backdating tool. When apply
creates a new canonical Payment, its `paid_at` is the owner-command confirmation
instant; where customer-subledger evidence is created, its `occurred_at` derives
from that Payment instant. Linking or replaying an existing canonical Payment
preserves its existing date. Neither intent creation nor an unauthenticated
historic Paystack timestamp becomes the accounting date. If finance requires a
historic-period restatement, stop and use a separately approved accounting-owner
process rather than this recovery command.

Use the canonical top-up reconciliation owner. Do not insert a payment, mutate
an invoice, add account credit, or unsuspend a subscriber directly. The owner is
idempotent across webhook delivery and scheduled recovery: replaying the same
provider transaction must reuse the canonical settlement rather than create
duplicate money.

The adapter accepts exactly one eligible terminal intent/reference pair. It has
no cohort, limit, date-range, provider-wide, or bulk-apply option. Pending intents
outside the configured automatic maximum-age window remain visible in
operational evidence but cannot be previewed or applied through this command;
they require a separate approved owner/policy decision. Eligible terminal rows
also remain visible until their canonical state changes. Preview does not remove
either class or make it scheduled work.

## Incident evidence

Record only non-secret evidence:

- Paystack reference and provider transaction identity;
- observed gross, fee, net, currency, and provider outcome;
- signed webhook receipt time, if present;
- reconciliation heartbeat and structured result;
- pending and terminal partition counts, oldest due ages, and selected/checked
  plus durable attempt progress;
- canonical payment and intent identifiers;
- exact recovery preview fingerprint, finance actor, reason, non-secret
  review/change reference and idempotency key;
- durable recovery-run identity and replay flag when apply succeeds;
- owner outcome or domain rejection code.

Never record signing secrets, credentials, raw private payloads, or unnecessary
customer identity data.
