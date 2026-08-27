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
  reconcilable-intent backlog projection. Its candidate policy uses a
  pending-first lane and a terminal late-success recovery lane; terminal rows
  may consume only leftover batch capacity after their retry cooldown is due.
- `financial.topup_intents` owns lifecycle transitions and the shared
  customer/admin blocker and retry projection, including typed gateway
  observation progress (`gateway_last_observed_at`, `gateway_last_outcome`,
  `gateway_last_reason_code`, `gateway_next_reconcile_at`, and
  `gateway_observation_count`). Provider adapters only normalize observations
  and never mutate intent state.
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

The scheduled fallback must not use one oldest-first queue for both pending
customer payments and terminal late-success audit. Pending unresolved gateway
intents are checked first once they are stale. Terminal failed, abandoned, and
expired intents are checked only when `gateway_next_reconcile_at` is due, using
the configured terminal retry interval. Non-success observations update the typed
progress fields while preserving the existing `metadata.gateway_verification`
evidence for compatibility.

The production Paystack webhook URL is:

```text
https://selfcare.dotmac.io/api/v1/payment-events/paystack
```

## Enable delivery

Set the URL above as the **live** webhook URL in the Paystack Dashboard/Canvas.
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
   - no eligible pending intents remain;
   - terminal recovery work is either due and bounded or cooling down;
   - no intents are stranded outside the automatic reconciliation window.
5. Verify the canonical payment records preserve the provider gross and fee,
   while account credit equals the authorized deposit amount.
6. Verify allocation, balance, and access changes are traceable to their named
   owners and were not written directly by the webhook adapter.

A `partial` reconciliation result is not success. Investigate its rejection
evidence even when the Celery task completed without raising an exception.

## Reconcile a stranded payment

Before requesting reconciliation, verify the exact customer, intent reference,
provider transaction identity, currency, gross amount, provider fee, and
authorized deposit amount. Fail closed on any ambiguity.

Use the canonical top-up reconciliation owner. Do not insert a payment, mutate
an invoice, add account credit, or unsuspend a subscriber directly. The owner is
idempotent across webhook delivery and scheduled recovery: replaying the same
provider transaction must reuse the canonical settlement rather than create
duplicate money.

Intents outside the configured automatic maximum-age window require explicit
finance review. The operational evidence reports them separately so they are
not mistaken for an empty or healthy queue.

## Incident evidence

Record only non-secret evidence:

- Paystack reference and provider transaction identity;
- observed gross, fee, net, currency, and provider outcome;
- signed webhook receipt time, if present;
- reconciliation heartbeat and structured result;
- canonical payment and intent identifiers;
- owner outcome or domain rejection code.

Never record signing secrets, credentials, raw private payloads, or unnecessary
customer identity data.
