# Ticket to Work-Order Handoff Source of Truth

Status: implemented

## Decision

A support ticket records an incident, its triage, the subscriber, and the team
accountable for the response. Field work is a separate operational command. An
active member of the ticket's active assigned service team must explicitly
issue each work-order scope. The HTTP adapters additionally require both
`support:ticket:update` and `operations:dispatch:write`.

`support.ticket_work_order_handoff`
(`app.services.ticket_work_order_handoff`) owns issuance eligibility,
idempotency, and native provenance. It delegates work-order creation to
`operations.work_order_commands`; dispatch and field execution continue to own
the resulting work order. One ticket may issue zero or many work orders.

The canonical relationship is the nullable, indexed foreign key
`work_order.origin_ticket_id -> support_tickets.id`. It uses `RESTRICT` on
delete so operational evidence cannot be orphaned. Generic work-order create
and update contracts expose the field for reads only and cannot establish or
change it.

## Lifecycle

1. Helpdesk captures and triages the incident in the ticket owner.
2. Helpdesk assigns the ticket to the appropriate service team.
3. An active member of that team explicitly issues a reasoned field scope with
   an idempotency key.
4. The work-order command owner creates a draft native work order and records
   the origin foreign key in the same transaction as ticket-side audit evidence.
5. Dispatch assigns the work and field operations execute it under the existing
   work-order owners.
6. Completion or unable-to-complete atomically projects an internal system
   comment back to the official ticket timeline with the work-order and field
   event identities.
7. Support verifies the result and separately decides whether to resolve or
   close the incident. Field completion never changes ticket status.

## Authority migration

Old owner/path: `app.services.support.Tickets._ensure_field_visit_work_order`
treated the `field_visit` tag as a command, created one work order, copied its
identity into `Ticket.metadata.work_order_id`, and stored the native ticket UUID
in the CRM provenance column `WorkOrder.crm_ticket_id`.

New owner/path: `support.ticket_work_order_handoff` decides explicit issuance;
`WorkOrder.origin_ticket_id` is the only native link.

Migration `382_ticket_work_order_handoff` backfills only work orders that carry
both sides of the retired automation evidence, clears the overloaded CRM value,
and removes the duplicate ticket/work-order metadata keys. The migration is
irreversible because a downgrade would restore parallel authority and a
one-work-order cache that cannot represent the new cardinality.

Cutover gates are:

- the tag-triggered method and both create/update call sites are absent;
- the generic work-order write schemas cannot accept `origin_ticket_id`;
- issuance enforces subscriber, team, membership, lifecycle, and idempotency;
- repeated issuance with the same key returns the same work order while a new
  key may create another;
- field outcome evidence is written exactly once with the field event and does
  not change ticket status;
- both ticket and dispatch projections link to the authoritative relationship.

There is no runtime fallback. Repair consists of reconciling the native foreign
key from retained audit evidence, never re-enabling tag automation or metadata
caches.
