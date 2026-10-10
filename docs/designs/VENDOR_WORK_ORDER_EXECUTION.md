# Native vendor work-order execution

Status: implementation candidate; Michael selected work-order support on 2026-10-10.

## Authority and scope

`operations.work_order_commands` owns organization-level vendor assignment in
the existing `WorkOrderAssignmentQueue`. An assigned queue record targets exactly
one active technician or native `Vendor`. `assigned_vendor_id` references
`vendors.id`, not the mobile-facing `FieldVendor` projection. WorkOrder header
labels are projections; neither header JSON nor imported metadata grants access.
Installation-project procurement remains a separate workflow.

`FieldVendor.native_vendor_id` is the explicit unique link from the mobile
membership projection to its authoritative vendor organization. Migration 668
backfills only exact existing organization-ID matches; unmatched projection
rows remain unlinked and cannot authorize field execution. The legacy string
field is not an input to the field work-order scope resolver.

`operations.field_work_order_access` resolves an immutable execution actor from
the authenticated SystemUser, active technician identity or active vendor
membership, native vendor linkage and current queue assignment. Ambiguous or
inactive identity fails closed. Vendor identity does not inherit technician
scope merely because the account also has a technician projection. No synthetic
technician profile is created.

Assignments lock the work order, revalidate the target and expected revision,
replace the opposing assignment, preserve an active execution lifecycle, stage
audit/event evidence and commit through the owner command boundary. Every
mutation checks assignment under the same work-order lock. Reassignment and
membership deactivation revoke access to reads, writes, replay and downloads.

## Execution and identity evidence

The supported vendor journey is assignment, Today, detail, Schedule,
start/pause/resume, work time, notes, photos/signature and completion. Existing
completion requirements remain authoritative. Events, worklogs, notes,
movements and attachments distinguish technician and vendor-member authors
using native foreign keys; nullable legacy person identity is not populated
with an invented identifier. Actor-scoped timer and retry lookup cannot match
unrelated NULL person identities. A retry with changed command content fails
closed rather than returning unrelated evidence.

Employee attendance, employee background tracking and ancillary operations
are separate capabilities. The Field read owner returns typed availability
and a safe reason for each. Mobile consumes that result, does not infer it
from role names, and does not mount unsupported background work or issue
unsupported ancillary requests. This slice does not enroll vendor users into
ERP payroll or claim vendor reimbursement or plant authoring support.

## Page contracts

### Dispatch assignment

- Screen: native work-order detail, staff dispatch audience.
- Decision: assign this exact work order to a technician or vendor organization.
- Identity: public work-order reference, title and current assignment.
- Owner: `operations.work_order_commands`; permission `operations:dispatch:assign`.
- First viewport: current work state and target identity; assigned work remains
  distinct from work creation.
- Action: select one target, review the owner preview, confirm with its revision.
- Refusals: inactive target, terminal work, stale revision or conflicting retry;
  show the safe owner reason and preserve the existing assignment.
- No bulk assignment, export or automatic project-to-work-order conversion.

### Field Today, Schedule and detail

- Audience: authenticated active vendor member assigned through the native queue.
- Owner: `operations.field_work_order_access` for scope; existing work-order read
  and completion owners for facts and transitions.
- Today: the existing server-filtered assigned work queue, status presentation,
  scheduled time, work-order reference and next execution action.
- Schedule: assigned work orders only; employee shifts and availability do not
  describe vendor organizations.
- Detail: same work-order identity and completion requirements as staff;
  customer/site context and evidence remain scoped to the assigned work.
- Actions: the supported execution journey above; unsupported capabilities
  display the owner reason and never invoke their command APIs.
- Loading, unavailable, empty and offline-cache states remain distinct. A failed
  read with no cache is retryable and cannot be rendered as zero assigned jobs.
- Profile: canonical `/api/v1/auth/me` account identity, explicit retry on
  failure and actual local queue counts only after successful queue reads.

## Migration, drift and rollback

Expand queue and evidence tables with native vendor foreign keys and actor
constraints. Validate legacy assigned rows before enforcing target constraints;
contradictory data stops migration with actionable evidence. Imported vendor
metadata is retained only as provenance and never automatically promoted into
an assignment. Review and reassign through the owner; no metadata fallback
survives cutover. Apply the database revision before the matching API/mobile
candidate. Existing technician assignments and evidence must remain valid.

Rehearse both a fresh real Alembic chain and the actual predecessor-to-head
upgrade on isolated PostgreSQL/PostGIS on the dedicated test server. Bound lock
and statement timeouts. Downgrade must refuse to discard vendor assignments or
evidence; forward-fix is the default once vendor execution exists.

Acceptance includes vendor-without-technician execution, cross-vendor denial,
deactivation and reassignment revocation, concurrent reassignment versus writes,
exact retry/conflict behavior, database actor/target constraints, metadata-only
denial, and unchanged technician execution. Source validation is not production
adoption; staging and exact-digest production authorization remain separate.
