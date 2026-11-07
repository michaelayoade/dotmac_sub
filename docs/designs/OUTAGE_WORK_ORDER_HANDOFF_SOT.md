# Shared outage work-order handoff

## Purpose

An infrastructure ticket represents one shared outage affecting many
customers. Staff can issue one field work order for that outage without
creating one customer work order per affected subscriber.

## Authority

- `network.outage_lifecycle` owns the incident and its immutable audience
  revisions.
- `network.outage_work_order_handoff` issues the canonical infrastructure ticket from the outage console, atomically binding it to the outage and routing it to the outage-response team.
- `support.ticket_lifecycle` owns the canonical infrastructure ticket.
- `network.outage_work_order_handoff` decides whether field work may be issued
  and records the outage-to-work-order link.
- `operations.work_order_commands` owns the native work-order row and dispatch
  lifecycle.

The work order has `work_order_kind=infrastructure` and no `subscriber_id`.
The link stores the incident, target, audience revision, and membership token.
The exact customer audience remains on the outage revision; it is not copied
onto the work order.

## Flow

1. Staff opens an active outage in Outage Console.
2. Staff selects **Create infrastructure ticket** on the open outage row. The command is idempotent, creates no subscriber link, and binds the canonical ticket to the outage.
3. The outage must have a current audience revision and a canonical active
   infrastructure ticket.
4. An active member of the ticket's assigned team enters the field instructions
   and submits the form.
5. The coordinator rechecks permissions, ticket/team membership, incident
   status, and the audience revision. A stale form is rejected.
6. The coordinator stages the infrastructure work order, provenance link,
   audit record, and event in one transaction.
7. Dispatch assigns and completes the work order through the existing field
   workflow. Completing it does not resolve the outage or ticket automatically.
8. Staff verifies restoration, then resolves the outage and closes the ticket
   through their existing owners.

## Support ticket form infrastructure selection

The staff support form offers an infrastructure typeahead for configured BTS,
cabinet, access point, and multiple-cabinet ticket categories. A selected BTS,
FDH cabinet, or network node is revalidated by the outage handoff owner. That
owner serializes creation on the selected inventory row, reuses an open incident
when present, or declares one with an immutable audience revision before
issuing the canonical customerless ticket. The preview shows only the count
from `network.outage_impact`; it does not expose the audience list. The typed
`network.infrastructure_ticket_selection` resolver owns the category-to-kind
policy and delegates inventory search and exact reference checks to
`network.infrastructure_catalogue`. Issuance locks the catalogue reference
and rechecks type eligibility before it declares or reuses an incident.

An operator can explicitly choose a manual name. The ticket owner
records this as unlinked evidence in ticket metadata. It does not create an
inventory record or outage incident, and the count is unknown. The manual
evidence remains on the ticket as the operator's description. No matching or
later inventory reconciliation is required by this workflow. Generic ticket
creation rejects these infrastructure categories even when old callers send
`base_station_details`; the typed manual ticket command or outage handoff
command is required. Manual creation serializes the request key and checks a
payload fingerprint, so an exact retry returns the existing ticket and reuse
of the key with different details fails closed.

Core Link Disconnection always asks the operator to type the affected link.
The operator may include endpoints and ports when known. Its affected-customer
count is shown as unable to determine.

The staff ticket-create page is the operator-facing form. Its primary decision
is to choose a category and either select a compatible inventory result or
explicitly enter a manual name. Search and impact preview require
monitoring read access; linked or manual infrastructure ticket submission
requires monitoring write and support ticket create access. The first viewport
shows ticket identity, chosen infrastructure, and count or "Unable to
determine". Empty search offers manual entry; a stale or deleted selection is
rejected by the command; unauthorized reads fail at the route. Desktop and
mobile render the same fields in one or two columns. Ticket detail and outage
console remain the drill-down destinations. The command audits the staff
principal and outage/ticket linkage; the manual ticket owner audits creation
and retains the entered infrastructure name.
The web adapter maps posted form values to `TicketFormValues`, `TicketCreate`,
and a typed existing or manual selection before invoking
`TicketFormCreateCommand`. The ticket owner admits these categories only from
the manual owner command or the outage handoff command.

Retries with the same request key replay the same work order. Reusing a key
with different instructions is rejected.
