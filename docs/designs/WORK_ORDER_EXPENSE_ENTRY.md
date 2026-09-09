# Work-Order Expense Entry

## Decision

Authenticated staff who can open an exact admin work-order detail page may
create an expense claim from that page after a technician has been assigned.
Technician assignment is an expense-eligibility requirement, not an identity or
access rule: global dispatch-read access, or a matching reseller or region
scoped grant, permits both viewing the page and creating a claim. Field/mobile
expense entry retains its assigned-technician scope.

Every claim created here is bound to the work order in the route. The browser
does not submit a work-order identifier and cannot create a work-order-less
claim. The authenticated session supplies the system-user and person identity;
requester identifiers and email are not form inputs.

## Owners and boundaries

- `ui.work_order_expense_projection` owns the typed form, field errors,
  requester-owned claim list, category help, action eligibility, and honest ERP
  delivery labels.
- `operations.expense_categories` owns the live ERP category observation,
  including receipt requirements and per-claim maximums. Unavailable and empty
  are distinct and both disable submission.
- `operations.expense_requests` owns validation and the atomic submitted claim,
  line items, requester evidence, idempotency fingerprint, receipt metadata,
  work-order assignment eligibility, activity mark, and durable ERP delivery
  staging. It rejects submission when the current work order has no assigned
  technician.
- The admin route parses HTTP form and file values, enforces CSRF and the exact
  work-order read RBAC scope, releases its read transaction, and invokes
  the typed owner command. It does not commit or call ERP.
- Sub is authoritative for the Field manager's approval or rejection decision.
  ERP accepts that trusted, evidenced decision without constructing a second
  approval chain. ERP remains authoritative for reimbursement bank details,
  payment intent state, transfer execution, reconciliation, and the final paid
  fact. The submission form deliberately has no approver, bank, cost-centre,
  ERP task, fleet vehicle, or receipt-number controls.

Receipt bytes use the existing private attachment storage owner. Metadata is
staged flush-only inside the expense command transaction. A deterministic
per-line receipt client reference makes a repeated claim submission safe. The
existing general field receipt endpoint remains assigned-technician scoped.
Staff uploader identity is recorded on `FieldAttachment`; the legacy
subscriber-only `StoredFile.uploaded_by` field remains empty for these staff
uploads.

## State and delivery semantics

The work-order card shows only claims created by the authenticated staff
identity. Local submission, durable delivery pending, delivered but awaiting
ERP acceptance, ERP accepted, rejected, failed/dead, approved, and paid facts
remain distinct. `sent` outbox evidence is not presented as ERP acceptance;
only an accepted event or ERP claim reference qualifies.

Expense delivery is ordered: submit creates the ERP `SUBMITTED` claim; approve
or reject waits for that create event; payment waits for the approval event.
Managers with the exact `operations:expense_request:pay` permission may stage a
payment command for an approved expense. The Field app never calls Paystack or
marks the claim paid. ERP creates and initiates the transfer, and Sub projects
`queued`, `pending`, `processing`, `indeterminate`, `failed`, `completed`, and
the resulting `paid` claim fact from ERP responses and polling.

The field app's expense list follows the same requester-owned rule. Ownership
is resolved from any exact technician-profile, canonical Person Party, or
authenticated SystemUser link on the claim. Work-order completion or
reassignment does not remove the claim from the requester's history, and a
claim created by another staff identity is not exposed.

## Schema change

`requested_by_technician_id` on expense requests and
`uploaded_by_technician_id` on field attachments become nullable. System-user
and person identity remain mandatory for new web submissions. The downgrade
fails closed while any staff-created rows without technician links exist.

## Validation and recovery

The server requires purpose, claim date, a three-letter currency, and one to 50
positive-amount lines. Each line requires an active ERP category and a
description of at most 500 characters. A receipt URL and receipt upload are
individually optional alternatives; when the selected ERP category requires
receipt evidence, either one satisfies that rule. The browser never marks the
file input itself as required. It changes the shared Receipt marker and help
text when the category changes, then validates the URL-or-file choice as one
requirement. Category receipt and maximum rules are enforced again by the
command owner. Browser calculations and required markers are assistance only.

Text values and the stable claim client reference survive validation errors.
Browsers cannot repopulate file inputs, so a selected receipt is cleared and an
explicit field error asks the user to reselect it. ERP category or sync
unavailability never fabricates a usable fallback.

Alembic revision `587_field_request_requester_history` repairs older claims
whose durable SystemUser link can be proven from their technician profile,
legacy SystemUser-as-person identifier, or unique Person Party binding. It also
adds requester lookup indexes. Ambiguous claims remain unchanged and hidden;
the repair never changes approval, delivery, or payment state and never queues
an ERP claim. `operations.expense_requests` owns this bounded, idempotent
repair, with Alembic acting only as its deployment adapter.

## Non-goals

This slice does not add a second persistence model, synchronous ERP requests,
in-app collection of bank credentials, automatic retry of indeterminate
transfers, historical claim backfill, or production cutover changes.
