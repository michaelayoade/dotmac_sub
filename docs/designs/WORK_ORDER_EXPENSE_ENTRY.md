# Work-Order Expense Entry

## Decision

Authenticated staff who can open an exact admin work-order detail page may
create an expense claim from that page after a technician has been assigned.
Technician assignment is an expense-eligibility requirement, not an identity or
access rule: global dispatch-read access, or a matching reseller or region
scoped grant, permits both viewing the page and creating a claim. Field/mobile
expense entry retains its assigned-technician scope.

Every claim is bound to an active authoritative work order by the expense owner.
The admin browser supplies no work-order identifier: the route is authoritative.
Field/mobile/API callers supply only the typed public ID, which the owner
resolves under assignment/access rules. Internal database UUIDs, placeholders,
generic drafts, and standalone expense creation are not accepted.

## Owners and boundaries

- `ui.work_order_expense_projection` owns the typed form, field errors,
  requester-owned claim list, category help, action eligibility, and honest ERP
  delivery labels.
- `operations.expense_categories` owns the live ERP category observation,
  including receipt requirements and per-claim maximums. Unavailable and empty
  are distinct and both disable submission.
- `operations.expense_requests` owns validation and the atomic submitted claim,
  line items, requester evidence, idempotency fingerprint, receipt metadata,
  work-order assignment eligibility, activity mark, and manager-approval ERP
  release. It rejects submission when the work order is inactive, unauthorized,
  unknown, or has no assigned technician. Submission creates no ERP event.
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

The reusable disclosure control is a semantic button with `aria-controls` and
synchronized `aria-expanded`. Activation opens the existing `details`, scrolls
the form into view, and focuses Purpose. Native `summary` mouse and keyboard
behavior remains available; permission-denied users receive a disabled button
and no submit surface.

## State and delivery semantics

The work-order card shows only claims created by the authenticated staff
identity. Local submission, durable delivery pending, delivered but awaiting
ERP acceptance, ERP accepted, rejected, failed/dead, approved, and paid facts
remain distinct. `sent` outbox evidence is not presented as ERP acceptance;
only an accepted event or ERP claim reference qualifies.

Manager approval is the sole ERP release point. Its single durable event creates
or retrieves an idempotent ERP draft, maps stable Sub line IDs to ERP items,
streams each private receipt from storage, uploads only missing attachments, and
delivers the trusted approval after all uploads succeed. Receipt bytes/base64
never enter the database outbox; supported URL receipts remain claim-line data.
Payment waits for the accepted approval-release event.
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

Private attachments must be active, owned by the same authoritative work order,
present in storage, within the receipt size limit, and have a supported MIME,
consistent size, and matching stored checksum. Receipt URLs must use a supported
HTTP(S) shape and cannot contain credentials, fragments, localhost, or literal
private-network addresses. Approval revalidates current category and receipt
evidence before staging delivery.

Text values and the stable claim client reference survive validation errors.
Browsers cannot repopulate file inputs, so a selected receipt is cleared and an
explicit field error asks the user to reselect it. ERP category or sync
unavailability never fabricates a usable fallback.

A dead approval-release event may be recovered only through a typed preview and
explicit recovery command. The owner revalidates approval, work order, category,
receipt, and ERP state, refuses ambiguity or a stale preview, preserves the
original event, and appends linked replacement evidence using a versioned key.
No automatic historical backfill or replay exists; legacy pre-approval events
are left untouched and refused by the worker.

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
