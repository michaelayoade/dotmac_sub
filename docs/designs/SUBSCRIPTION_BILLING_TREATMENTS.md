# Subscription Billing Treatments

Status: implementation complete on `feat/subscription-billing-treatment`; cutover
requires migration, validation, review, and merge.

Owner: `financial.subscription_billing_treatments`

Grant participant: `financial.subscription_billing_grants`

## Decision

A catalog offer describes a product and its economic price. It does not encode
why one customer receives service without paying Dotmac. Selected complimentary
or sponsored service is therefore one explicit, effective-dated
`SubscriptionBillingArrangement` scoped to the subscription. Standard billing
is the absence of an effective non-standard arrangement.

A zero-price catalog offer is valid only for a product genuinely free to every
eligible subscriber. `Subscription.unit_price = 0`,
`Subscriber.billing_enabled = false`, future billing anchors, grace overrides,
manual activation, and account status are not complimentary-service authority.
Zero-price offers remain administrative catalog records but are excluded from
customer self-service upgrades, downgrades, and cross-family migrations. A
support/admin workflow may assign a genuinely free product under its separate
eligibility policy; account-specific concessions use this treatment owner.

## Authoritative records

An arrangement records the subscription, account, authorized offer, treatment,
reason, interval, positive recurring-value ceiling, currency, billing cadence,
sponsor/cost-centre evidence, and approval/revocation command audit. The real
offer and contracted value remain unchanged. Overlap is rejected under the
subscription lock. Plan changes require revocation and reapproval against the
new offer and value. Treatment starts and ends must fall on complete billing
boundaries, so approval never silently waives an earlier part of a cycle.

Every arrangement has a mandatory end. The registered billing setting
`subscription_billing_treatment_max_days` owns the approval horizon. Its
default and upper bound are 366 days, allowing one leap-year-safe annual review;
operators may shorten the horizon but cannot create a permanent exemption.
Renewal is a new reviewed approval with new command and audit evidence, not an
automatic extension. Each arrangement snapshots the effective
`approval_policy_max_days`, preserving why it satisfied policy even if the
registered setting is shortened later.

While an effective or scheduled arrangement remains open, both the application
owner and a PostgreSQL trigger reject changes to the subscription's offer,
offer version, billing mode, cadence, unit price, or discount terms. Revoke the
arrangement, change the contract, and approve a new arrangement instead.

`SubscriptionBillingGrant` is append-only evidence for one exact approved
period. Its deterministic key is the arrangement, subscription, start, and end.
It atomically creates or repairs a grant-linked `ServiceEntitlement`, advances
the billing anchor, and emits `subscription_service.granted`. It never creates a
customer invoice, debit, payment, credit, or wallet mutation.

## Resolution and recurring behavior

The resolver returns:

- `standard`: ordinary customer billing applies;
- `effective`: customer billing is suppressed and an exact grant may be made;
- `protected_drift`: contradictory account, offer, price, currency, or cadence
  evidence suppresses charging but cannot fabricate coverage.

Postpaid invoice generation and prepaid renewal consume this same decision.
The prepaid threshold excludes effective and protected non-standard services
and reports their exact `non_billable_subscription_ids`. This classification is
separate from customer-funded coverage.

Expiry resumes standard billing from the last exact grant boundary. Revocation
does not erase granted periods or automatically waive older debt. Non-cash
grants do not depend on the prepaid funding-position cutover, which owns
customer-money debits only.

## Sponsored service

Sponsored treatment requires a sponsor reference or internal cost centre. The
reference value remains reportable. Charging an external sponsor or posting an
internal allocation belongs to a separate approved ERP/accounting contract; it
must not be simulated with a customer invoice or zero catalog price.

## Administrative contract

The authenticated API exposes preview, confirm, list, and revoke under
`/api/v1/billing-treatments`. Preview confirmation is fingerprint-bound. Writes
require `billing:treatment:write`; reads require `billing:treatment:read`.
Preview fails closed when the requested end is absent, exceeds the registered
approval horizon, or does not align with the subscription cadence.

## Migration and cutover

Legacy exceptions are not automatically backfilled because a zero price,
disabled billing flag, long grace, or future anchor does not prove approval.
Genuinely free catalog products may retain a zero recurring price; zero price is
retired only as an account-specific concession mechanism.

1. Apply migration `398_subscription_billing_treatments`.
2. Grant the new permissions narrowly.
3. Report and review legacy zero-price/billing-disabled/grace/anchor candidates.
4. Approve valid cases through the owner command.
5. Run controlled prepaid and postpaid cycles.
6. Verify the grant, entitlement, anchor, no customer-money delta, and access.
7. Retire each reviewed legacy exception only after new evidence is valid.

Rollback is forward-fix: approval, grant, entitlement, audit, and event evidence
is retained. Replaying the exact grant is idempotent and repairs a missing
entitlement or stale anchor without duplicating the grant.
