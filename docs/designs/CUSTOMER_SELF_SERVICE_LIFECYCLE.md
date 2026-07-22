# Customer Self-Service Lifecycle

Status: approved target; migration in progress

## Contract

```text
customer review
  -> server-owned preview and eligibility
  -> confirmed, idempotent intent
  -> canonical commercial/lifecycle command
  -> billing and access consequences
  -> delivery-mode branch
      -> commercial-only apply
      -> remote reprovision + verification
      -> field fulfillment + verification
  -> verified operational state
  -> Customer 360, portal, reseller, mobile, and timeline projection
```

Sub is authoritative. Web routes, APIs, templates, mobile clients, support
tickets, work orders, RADIUS, and equipment controllers are adapters or
downstream records; none may independently decide the customer lifecycle.

## Owners

| Decision or fact | Owner |
| --- | --- |
| Subscription state, command preview, billing/access impact, delivery mode, relocation qualification and field-fee preview | `service_intent.subscription_lifecycle` |
| Locked command execution and idempotent outcome | `service_intent.subscription_lifecycle_execution` |
| Plan-change request intent and result evidence | `app.services.subscription_changes` |
| Prepaid plan-change pricing/funding evidence | `financial.prepaid_plan_change` |
| Account/subscription access state | `access.subscription_lifecycle` |
| RADIUS session projection | `network.radius_sessions` |
| Connection/outage diagnosis | `network.connection_health` |
| Support-to-field handoff | `support.ticket_work_order_handoff` |
| Work-order execution | `operations.work_order_commands` |
| Provisioning readiness/result | `operations.provisioning_lifecycle` |
| Cross-surface read projection | `ui.portal_account_health_projection` |

## Change classes

Every compatible upgrade and downgrade uses a reviewed service-change preview
and the subscription lifecycle command owner. The subscription, exact
financial adjustment, access/profile consequence, request evidence, audit, and
event must converge in one idempotent outcome.

`plan_family` is commercial merchandising policy and never proves that a site
visit or access-network migration is required. The service-change owner derives
one delivery mode from provisionable catalog and current-service facts:

- `commercial_only`: no network intent changes; apply without provisioning;
- `remote_reprovision`: the access medium is unchanged but speed/profile intent
  changes; persist the confirmed intent, then execute and verify remote
  provisioning without a ticket or work order;
- `field_migration`: the physical access medium or another field-only fact
  changes; persist the confirmed intent, then create fulfillment scope and a
  work order.

A service-address change is always `field_migration`, including when the
customer keeps the current offer. For fixed-wireless/radio access the owner
must qualify the exact target address and resolve a nonzero one-time field fee
from the catalog offer selected by
`projects.wireless_relocation_offer_id`. Missing coordinates, an ineligible
qualification, an absent/inactive offer, or a missing/nonpositive one-time
price blocks confirmation. Templates and mobile clients render this result;
they never infer serviceability or calculate the fee.

The field fee is separate from recurring-plan proration. Confirmation persists
the target address, qualification evidence, exact fee offer/amount/currency,
and quote fingerprint on `SubscriptionChangeRequest`. A priced relocation
enters `awaiting_payment`; the current subscription offer and service address
remain unchanged. Fee settlement must precede the service-order/work-order
handoff, and only verified field provisioning may request the final
subscription/address change.

Support tickets are exception/triage collaboration only. They are not a
mandatory step in a planned service change. If support becomes involved, the
ticket links structurally to the service-change intent; it does not own it.
Field completion does not itself change the plan: verified provisioning
evidence must satisfy the service-change owner before the subscription command
is executed.

Vacation hold and resume use the subscription lifecycle owner with the
customer-hold policy and exact enforcement-lock evidence. Device reboot and
Wi-Fi changes remain network command intents scoped to the customer's exact
subscription and assigned device; page rendering never performs equipment
polling or writes device state.

## Projection

`ui.portal_account_health_projection` is the shared first-view read contract.
For every operationally-current service it shows lifecycle, access, session,
connection/outage evidence, freshness, next action, and any pending plan or
network change. Customer Portal, Reseller Portal, Customer 360, and mobile
consume the same DTO.

## Migration and retirement

Completed in this slice:

- Customer 360 consumes the canonical service-health strip.
- Active `SubscriptionChangeRequest` state is batch-projected into Account
  Health and mobile.
- Every compatible customer upgrade and downgrade is classified by provisionable
  facts through `service_intent.subscription_lifecycle_execution`; the portal
  adapter retains only customer scope, catalog exposure, preview display, and
  transport-error mapping.
- Commercial-only changes apply immediately. Remote and field changes persist a
  reviewed, idempotent `SubscriptionChangeRequest` without changing the current
  subscription, opening a ticket, or creating unverified billing effects.
- The former plan-family filter, support-ticket fallback, migration endpoint,
  and duplicate migration offer list are retired.
- Customer self-service API routes are `/service-change`; the former
  `/plan-change` paths are retired without compatibility aliases.
- Customer Portal, Reseller Portal, `/api/v1/me`, reseller API, and mobile all
  use the same address-scoped preview and confirmation owner. The reseller
  adapter proves managed-account scope before invoking it.
- Address-only relocation is supported without manufacturing a plan change.
- Fixed-wireless/radio relocation is serviceability-qualified and catalog-
  priced, and confirmation remains `awaiting_payment` without prematurely
  changing the subscription.

Still required before this lifecycle is complete:

- implement the remote provisioning verifier and the field fulfillment/service
  order handoff; for a priced relocation this includes invoice/payment evidence,
  then a service order and work order, and finally a subscription command only
  after verified delivery;
- route vacation hold/resume through the lifecycle command owner;
- expose scoped reboot/Wi-Fi command outcomes consistently on web and mobile;
- remove the superseded customer-route decisions and add repair/audit tooling.

No compatibility response or fallback will be retained after each in-repository
consumer is migrated and the cutover gate is green.
