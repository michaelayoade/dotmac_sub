# Shared outage work-order handoff

## Purpose

An asset-linked infrastructure ticket represents one shared outage affecting
many customers. A manually named infrastructure ticket has no outage. Both can
issue customerless field work orders into the shared dispatch queue.

## Authority

- `network.outage_lifecycle` owns the incident and its immutable audience
  revisions.
- `network.outage_work_order_handoff` issues the canonical infrastructure ticket from the outage console, atomically binding it to the outage. Console issuance uses the outage-response team; creation from the support form retains the selected active service team.
- If an outage already has a canonical ticket assigned to another team, support-form creation reports a conflict instead of changing or silently ignoring the selected team.
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
4. An active assigned-team member or global administrator with monitoring and
   dispatch-write permissions enters the field instructions from the ticket
   detail page or outage console.
5. The coordinator rechecks permissions, active team and staff identity, incident
   status, and the audience revision. A stale form is rejected.
6. The coordinator stages the infrastructure work order, provenance link,
   audit record, and event in one transaction.
7. Dispatch assigns and completes the work order through the existing field
   workflow. Completing it does not resolve the outage or ticket automatically.
8. Staff verifies restoration, then resolves the outage and closes the ticket
   through their existing owners.

For a manually named infrastructure ticket, the ticket detail action validates
the saved explicit manual selection or current manual-kind policy for legacy
tickets, active ticket and assigned team, and stages a
native infrastructure work order with the ticket as origin. The operator
confirms or enters the affected infrastructure name in the modal; the work
order stores it and displays it on the dispatch detail page. Older customerless
tickets without selection metadata use this path when their configured ticket
type permits a manual target. Saved site details prefill the name; otherwise
the operator must enter it. The ticket title alone is not used as target
evidence. There is no fabricated outage link or customer audience. Creation
and technician assignment remain separate commands. A dispatch staff member
can open the created order from the ticket to assign a technician. The manual
path locks and rechecks the ticket, then records ticket-scoped audit and event
evidence once per idempotency key.

## Support ticket form infrastructure selection

Ticket Settings owns the editable category-to-target policy. The ticket-type
list supplies rows; staff marks each infrastructure type and its allowed target
kinds (base station, cabinet, network node, access point, or manual). The
existing six categories retain their manual-name fallback as defaults until
settings are saved, after which the saved policy is authoritative, including
an empty policy. These default categories appear in the ticket type selector
even before settings are saved. Removing a category's manual kind stops new
manual work-order issuance from legacy tickets of that category. Tickets with
explicit saved manual selection remain infrastructure tickets, and linked
outage tickets continue through their canonical outage path. A selected BTS,
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
creation of customerless tickets rejects these infrastructure categories even
when old callers send `base_station_details`; the typed manual ticket command
or outage handoff command is required. Generic API/customer tickets with an
explicit customer target retain their customer work-order path even if their
category is marked for infrastructure. Manual creation serializes the request key and checks a
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
