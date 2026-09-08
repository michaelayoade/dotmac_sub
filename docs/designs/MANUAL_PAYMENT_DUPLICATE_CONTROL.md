# Administrative Manual-Payment Duplicate Control

## Problem

The generic staff payment form can create a succeeded payment without a bank
or receipt reference. Its legacy sixty-second same-account/same-amount guard
only protects against a rapid double click. It neither detects an older manual
entry nor warns that the customer already has a submitted payment proof. A
reviewer can therefore represent one transfer two or three times.

## Authority and boundary

`financial.payments` remains the sole owner of Payment, settlement, allocation,
ledger, and `payment.received` evidence. `financial.payment_proofs` remains the
sole owner of PaymentProof review. Neither owner depends on the other.

`financial.manual_payment_recording` is an application coordinator above both
owners. It owns only the staff decision that the proposed manual payment is not
duplicate evidence. Confirm uses one coordinator-managed transaction and the
flush-only `financial.payments` participant. The adapter enters the owner
command with typed actor, scope, reason, correlation, and idempotency evidence.

## Decision rules

For a succeeded administrative payment:

1. A non-blank bank transaction ID or cash receipt reference is required.
2. The normalized reference must not exist on any Payment for the same account
   and currency.
3. The normalized reference must not exist on a submitted PaymentProof for the
   same account and currency. Staff must review that proof instead.
4. Up to five active, non-failed/non-canceled payments from the preceding 90
   days with the same account, currency, and amount are displayed as risks.
5. Up to five submitted proofs with the same account, currency, and amount are
   displayed as risks.
6. Same-amount risks do not prove duplication: recurring charges and split
   deposits are legitimate. They require explicit acknowledgement rather than
   a hard block.

Proof verification already locks the subscriber account. After acquiring that
lock it also rejects a proof reference already represented by an account
Payment. This makes the cross-route rule symmetric: whichever of manual
recording or proof verification wins the lock, the later route cannot create a
second payment for the reference.

Pending payment intents have no money effect, so they may omit the reference
and do not require duplicate acknowledgement.

## Preview and confirmation

Preview combines the canonical payment financial preview with a bounded
duplicate-risk assessment. A control fingerprint binds the financial preview,
normalized reference, and displayed evidence IDs/statuses/amounts/timestamps.

Confirmation locks the Subscriber account first, rebuilds both previews, and
fails closed if either fingerprint changed or acknowledgement is missing. The
same account lock serializes competing manual confirmations so two requests
cannot both pass an account-scoped reference check. Exact idempotent replay
returns the original payment before it can match itself and emits no duplicate
audit or event.

The committed transaction contains the canonical payment effects, the payment
owner's event/audit evidence, a coordinator audit recording the reviewed risk
IDs, and `manual_payment.recorded`. References and customer identity are not
copied into the coordinator event payload.

## User interface

The admin form names the required reference and explains its purpose. Review
shows exact financial consequences plus links to every matching payment/proof.
When risks exist, the confirmation checkbox is both browser-required and
server-enforced. API callers use the same preview/control fingerprints and
acknowledgement field; the web page is not an enforcement boundary.

## Compatibility and rollout

The existing `Payment.external_id` column stores the reference, so no database
migration is required. Non-administrative payment ingress and provider-event
owners keep their existing paths. The generic legacy payment confirmation
wrapper remains available to those callers, while admin web and administrative
creation API routes cut over to the coordinator.

Validation covers missing/reused references, submitted-proof conflicts,
same-amount warnings, acknowledgement, stale evidence, idempotent replay,
transaction ownership, adapter routing, templates, and the SOT registry.
