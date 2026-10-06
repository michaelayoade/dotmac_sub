# Subscription pause lifecycle

Status: implementation contract

## Meaning

`paused` is a first-class Subscription and derived Subscriber status. It means
normal service access and recurring service-period consumption are stopped,
while the customer's service identity and configuration are preserved. It is
not an alias for `suspended`, `blocked`, `disabled`, or `canceled`.

The pre-existing administrative Disable/Restore workflow remains a separate
legacy lifecycle contract. It cannot create or release a pause cause and is
not used by the Ticket-SLA Automation or customer-vacation workflows.

| Concern | Paused contract |
| --- | --- |
| Access | denied |
| Billing period | stops at the episode effective time |
| Credentials, IP, devices and offer | preserved |
| Account status | derived `paused` |
| Customer portal | remains available and explains the pause |
| Resume | cause-specific: authorized administrator, customer request, or scheduled customer-vacation instant |
| Compensation | exact effective pause duration, once |

## Ownership

`access.subscription_lifecycle` owns status transitions, pause episodes and
causes, account projection, the typed prepaid pause-compensation entitlement,
billing-anchor adjustment, lifecycle evidence, and events.
`support.ticket_sla_service_consequence` owns revalidating Ticket/SLA evidence
and coordinating the SLA cause, including typed resume eligibility previews.
`financial.prepaid_service_coverage` owns the exact coverage decision consumed
by those previews and by the lifecycle participant; it treats funded
entitlements and applied service-extension grant intervals as the same
authoritative coverage union without rewriting either source.
Automation adapters, routes, event handlers,
and templates do not write lifecycle or billing state.

One episode represents `[effective_at, resumed_at)`. Multiple typed causes may
hold it open. Source identity and idempotency keys make pause replay converge;
cause release is independent; the last release closes the episode. Only
`active -> paused` begins an episode. Final release results in `active` when no
independent enforcement lock remains, otherwise `suspended`.

`suspended` is the enforcement-hold contract: access and future recurring
billing stop, but the unused part of the current period is not preserved and
restore does not move the billing anchor. Existing invoices remain due facts.
`disabled` remains the separate legacy reversible administrative stop contract:
billing and access stop, and restore shifts the billing anchor by the disabled
duration. `paused` is the cause-backed clock freeze defined here. `canceled` is
terminal. `blocked` and derived `delinquent` remain the recoverable collections
states. Operators must select the state that matches the customer's intent
instead of treating these labels as interchangeable.

## SLA workflow

1. The SLA owner records a real resolution breach and stages the durable event.
2. A published Automation rule selects the typed pause action and policies.
3. The coordinator revalidates an unresolved Ticket and one active service.
4. The lifecycle owner creates the episode/cause and transitions to `paused`.
5. The customer receives a durable notification; network projection denies new
   sessions and disconnects current sessions after commit.
6. At `pending_confirmation` or `closed`, an administrator previews resume.
7. Confirmation rechecks the fingerprint, releases the cause, moves the
   billing anchor by the exact interval once, and restores access if eligible.
   For prepaid service, the same transaction creates one zero-value
   `ServiceEntitlement` linked uniquely to the pause episode for
   `[previous_next_billing_at, resulting_next_billing_at)`. The original paid
   entitlement, applied extension, and invoice period remain immutable. The
   complete interval from pause effective time through the captured anchor must
   be proved by the canonical union of funded entitlement and applied
   service-extension intervals. Missing, discontinuous, or anchor-inconsistent
   prepaid coverage evidence fails closed.

## Customer vacation workflow

1. The portal asks the lifecycle policy for vacation eligibility, annual-use
   limits, cooldown, and duration limits.
2. Confirmation creates a typed `customer_vacation_hold` cause on the active
   pause episode and records `scheduled_resume_at`; it does not create an
   `EnforcementLock`.
3. The subscription and derived account project as `paused`. Normal access and
   recurring service-period consumption stop while configuration is retained.
4. The customer may release the exact cause early. Otherwise the scheduled
   adapter releases it after `scheduled_resume_at` through the same lifecycle
   command owner.
5. Final release extends `next_billing_at` by the exact elapsed pause duration
   once. For prepaid service, the corresponding zero-value compensation
   entitlement preserves the unused paid interval without rewriting the paid
   invoice.

Historical `customer_hold` enforcement locks remain readable only for annual
usage/cooldown evidence. No new vacation workflow may create one, and an active
legacy lock is not renewable prepaid lifecycle evidence.

## Administrative pause workflow

The admin subscription action presents Suspend and Pause as separate commands.
Suspend creates a reason-scoped enforcement lock and stops access and future
recurring billing without preserving unused time. Pause creates an
`administrative` pause cause, denies access, stops collection, and preserves the
unused service period. The confirmation preview and action label state this
difference explicitly so an operator cannot reasonably treat the two commands
as synonyms.

Administrative resume releases only the exact active `administrative` cause.
Final release extends `next_billing_at` by the exact effective pause duration
through the same billing-anchor owner used by Ticket-SLA and customer-vacation
pauses. It never clears an enforcement lock; if an independent restriction is
active, the resulting service remains `suspended`.

## Configuration and invariants

Customer scope, SLA targets, workflow publication, service-selection policy,
resume policy, billing treatment, and notification delivery are configured by
their existing database-backed owners. Code contains only closed typed domain
values and safety invariants. The example 30-day service period is never a
constant: the existing subscription billing anchor/cadence is authoritative.

Deployment adds the status values, evidence tables, scheduled customer resume
field, permissions, capabilities, templates, and readers. It does not publish
a Ticket-SLA workflow, grant permissions, or silently reinterpret an
administratively suspended service as a pause.
