# Customer Experience Lifecycle Source of Truth

Provisioning now joins this graph through native `ServiceOrder.project_id` and
`ServiceOrder.activation_project_task_id`. The customer project projection
includes the latest persisted provisioning readiness decision and named checks;
it never re-evaluates activation policy. See
`docs/designs/PROVISIONING_LIFECYCLE_SOT.md`.

Status: implemented by migration `386_customer_experience_lifecycle_sot`.

## Scope

This contract traces a Sub-owned customer work journey from planned project
work through field execution, support verification, customer confirmation, and
feedback. It is used by staff operations, the field app, the customer mobile
app, the customer web portal, and reseller self-care.

CRM is not a project or work-order authority in this flow. Historical
`crm_*` values on native roots are provenance only. The project/work-order
mirror tables, live pull/webhook tasks, read flips, and fallback paths are
retired. Dotmac CRM connector operations for project, work-order, work-order
note, and technician-location reads are removed as part of the same cutover.

## Native relationship graph

```text
Subscriber
  └─ Project (0..n)
       └─ ProjectTask (0..n)
            ├─ Ticket (0..1 planning/incident context)
            └─ WorkOrder (0..n field visits)
                 └─ origin Ticket (0..1 issuance provenance)
```

The stored keys are:

| From | Key | To | Cardinality | Canonical writer |
| --- | --- | --- | --- | --- |
| Project | `subscriber_id` | Subscriber | customer 1, projects 0..n | `operations.project_lifecycle` |
| ProjectTask | `project_id` | Project | project 1, tasks 0..n | `operations.project_lifecycle` |
| ProjectTask | `ticket_id` | Ticket | task 0..1 ticket | `operations.project_lifecycle` |
| WorkOrder | `project_id` | Project | visit 0..1 project | `operations.work_order_commands` |
| WorkOrder | `project_task_id` | ProjectTask | visit 0..1 task; task 0..n visits | `operations.work_order_commands` |
| WorkOrder | `origin_ticket_id` | Ticket | visit 0..1 issuing ticket; ticket 0..n visits | `support.ticket_work_order_handoff` through the work-order command owner |

`WorkOrder.id` is the internal UUID FK target. `WorkOrder.public_id` is the
external job identity used by field and self-care routes. A CRM identifier is
never a native join key.

## Lifecycle and decision ownership

| Journey step | Authoritative state/decision | Owner | Customer projection/action | Communication intent |
| --- | --- | --- | --- | --- |
| Project accepted/planned | Project status, dates, task plan | `operations.project_lifecycle` | Installation tracker and server status presentation | Project/task milestone intents |
| Task needs field action | Explicit work scope and optional ticket context | Project owner; ticket handoff when incident-led | Task shows linked ticket and zero or more visits | None until a named consequence occurs |
| Ticket issues field visit | Team membership, ticket eligibility, idempotent issuance | `support.ticket_work_order_handoff` | Visit appears under task/ticket | Work-order lifecycle intents begin at field events |
| Technician dispatched/working | Assignment and work-order header projection | `operations.work_order_commands`; field transition owner for execution status | Track action only when server projects it | `work_order_en_route`, `work_order_arrived` via direct WhatsApp + push defaults |
| Field outcome | Evidence gate and complete/unable transition | `operations.field_completion` | Visit outcome and typed project/task/ticket context | `work_order_complete` or `work_order_unable_to_complete` via email + direct WhatsApp + push defaults |
| Ticket verification | Whether field evidence resolves the incident | `support.ticket_lifecycle` | No automatic ticket closure | Resolution confirmation request |
| Customer response | Confirm closes; dispute reopens with reason | `support.ticket_lifecycle` | Authenticated portal/mobile and signed link use the same active capability | Existing ticket lifecycle consequences |
| Feedback | Technician rating; support CSAT | `customer.work_order_selfcare`; `support.ticket_lifecycle` | Rate actions only when server projects eligibility | No implicit lifecycle mutation |

Field completion never auto-completes a project task and never resolves or
closes a ticket. It appends an official ticket timeline fact when the work order
has `origin_ticket_id`. The responsible support team verifies the outcome and
requests customer confirmation. A customer dispute reopens the incident; it
does not create a parallel ticket.

## Read projection

`customer.experience_lifecycle`
(`app/services/customer_experience_lifecycle.py`) is a read-only composer. It
derives:

- typed UUID relationships and the public work-order id;
- canonical status presentations;
- project stage progress and current stage;
- customer experience state (`planned`, `in_progress`, `field_work`,
  `waiting_on_customer`, `on_hold`, `resolved`, or `canceled`);
- allowed customer actions with method and API path;
- project-task tickets, field visits, and origin tickets.

The owner writes nothing. Project, work-order, field-transition, and ticket
services remain the decision owners. API/web/reseller adapters only scope the
caller and return the projection.

Field job detail exposes `customer_experience.project`, `project_task`,
`origin_ticket`, and `project_task_ticket` with native UUIDs and canonical
presentations. It does not expose CRM project/ticket strings as relationship
state.

## Self-care commands

The server projects action eligibility; clients do not infer it from a raw
status. Current action keys are:

- `view_project`, `view_work_order`, and `view_ticket`;
- `track_technician` and `rate_technician`;
- `confirm_resolution`, `dispute_resolution`, and `rate_support`;
- `contact_support`.

Technician tracking reads the current native assigned technician and Sub field
presence. Technician rating is subscriber-scoped, audited, and idempotent: the
first accepted rating is canonical.

Authenticated resolution actions require an active, unexpired confirmation
capability. They delegate to the same confirm/dispute functions as the signed
public link. Confirmation closes the ticket; dispute reopens it and retains the
customer reason.

## Communications

Domain owners request named outcomes through
`communications.customer_experience_intents`; they do not invoke email,
WhatsApp, SMS, or push transports.

Every intent includes a stable dedupe key plus the available native lineage:
subscriber, project, project task, work-order UUID/public id, ticket, and field
event. `communications.intents` expands the subscriber and authorized contacts,
applies event/channel policy, preferences, suppression and account policy, and
creates delivery rows. Direct WhatsApp is the message channel; there is no
Twilio or fallback decision path.

Transport attempts and delivery status remain communication state. They cannot
advance a project, work order, task, or ticket.

## Cutover and repair

Migration 383:

1. adds `work_order.project_task_id` and backfills retained task links;
2. fails closed on unresolved links or project conflicts;
3. drops `project_tasks.work_order_id`, making the one-to-many direction
   explicit;
4. fails closed if a project mirror record has no subscriber-matching native
   Project;
5. removes project/work-order mirror and sync-state tables, retired controls,
   webhooks, scheduled pulls, and CRM connector observation operations;
6. has no downgrade because restoring parallel authority would recreate drift.

Reconciliation is therefore a native relationship repair, not a CRM refresh.
Callers correct the owning ProjectTask, WorkOrder, or Ticket state through its
command owner and then re-read the lifecycle projection.
