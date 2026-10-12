# Work-order purpose and access technology catalog

Status: explicit opt-in classification and read-only recommendation catalog.
Execution checklist enforcement remains separate integration work.

`operations.work_order_templates` owns explicit purpose/technology compatibility
and versioned approved field-scope recommendations. The immutable query and
outcome live in `app.services.work_order_templates`; the executable declaration
is the provisioning-operations `work_order_templates` registry shard.

Purpose is independent of native `WorkOrderKind` (`customer` or `infrastructure`)
and access technology (`fiber`, `radio`, `unknown`). Customer deployment requires
customer kind; infrastructure rollout requires infrastructure kind. Repair,
maintenance, upgrade and relocation support either kind. Fiber and radio support
all compatible purposes. Unknown technology supports repair/maintenance diagnosis
only; deployment, rollout, upgrade and relocation require explicit fiber/radio.
Shared infrastructure is represented by the existing `infrastructure` kind,
including project rollout; this slice does not invent a parallel kind.

Every selection uses `ExplicitWorkOrderClassification` and explicit operator
provenance. Legacy creation remains unchanged and unclassified. The explicit
second step is `operations.work_order_commands.classify`, with the immutable
`ClassifyWorkOrderCommand` carrying native UUID, expected aware revision,
classification and `CommandContext`. The command enters the canonical executor
once, locks the native work order, validates active draft/scheduled state before
execution, and rejects stale revisions. Under that lock the immutable audit
receipt serializes idempotent retries: an identical identity/payload returns the
original accepted revision; changed payloads conflict. It records the four
classification columns, catalog version, audit evidence and `work_order.classified`
event in one transaction. The dedicated event has no execution or activation
automation capability in this slice. No evidence or lifecycle transition follows from the
selection. Existing creators, header updates and participant transactions are
untouched; no compatibility exception or new creation writer is introduced.

`POST /dispatch/work-orders/{work_order_id}/classification` accepts the internal
native UUID (the existing create/read header supplies `id`), requires dispatch
write permission and a command UUID, expected revision, explicit classification
and reason. Its typed response exposes the recommendation-only requirements and
accepted revision. Existing work-order header reads expose the nullable persisted
purpose, technology, catalog version and provenance. The user flow is create, then explicitly classify from the admin detail link
or through this endpoint; the admin create form is unchanged. A single create-and-classify UI requires a later complete typed
migration of the legacy creation boundary.

Migration `673_explicit_work_order_classification` follows
`672_field_identity_review`, adds nullable fields, and enforces all-or-none,
enum, kind and technology compatibility in the database. Every historical row
remains NULL. Never infer purpose or technology from `work_type`, titles, tags,
ticket text, project labels or metadata; never backfill by a string heuristic.
The additive DDL takes normal PostgreSQL table locks. Revision-local lock and
statement budgets are 5s and 120s, respectively,
following existing repository migration budgets. Retry a failed transaction
whole in an approved maintenance window. No backfill or data-volume scan is
introduced beyond constraint validation. Downgrade refuses to discard explicit operational
classification; forward repair is required. Fresh and predecessor PostgreSQL
migration rehearsals remain required before release; local unit results do not
prove deployed-schema acceptance.

The catalog recommends scope/target, supervisor dispatch, safety/permits,
materials, purpose-specific planning, technology-specific work/tests, as-built
and handover evidence. Fiber recommendations name route/core/port/splice plans
and applicable optical power/continuity/OTDR evidence. Radio recommendations name
line of sight, spectrum/link design, mounting/grounding/power and applicable
alignment/interference/link/service test evidence. Site-specific acceptance
criteria belong to approved engineering plans and their network owners; this
catalog invents no numeric thresholds.

`WorkOrderTemplateRecommendation.use` is always `recommendation_only`. The gate
labels describe when evidence is recommended, not an enforced workflow status.
Unknown technology recommends a survey and approved engineering/test plan before
technology-specific work. Catalog version 1 is deterministic at the checked-in
revision; changed requirements require an explicit catalog version change.
A recommendation proves neither an installed asset nor accepted test evidence.

`operations.work_order_commands` retains native creation/header and assignment
ownership; the existing dispatch and authenticated field owners retain technician
and vendor access/execution. Supervisor review and explicit dispatch are required
planning steps, not automatic assignments. Ticket handoff and outage/project
coordinators retain their origin, authorization and idempotency decisions.
Field evidence and completion, topology/inventory facts, service acceptance,
provisioning, billing and ticket closure remain decisions of their existing
owners. Selecting a template performs none of those transitions. Enforcing a
recommended gate requires a separately integrated typed owner contract and
actual evidence; clients must not recalculate or claim enforcement.

Validation covers the full compatible kind/purpose/technology matrix, rejects
incompatible and unknown-technology installation choices, distinguishes fiber
and radio evidence, preserves immutable deterministic outcomes and checks
registry ownership. The recommendation catalog itself has no database state or
external effects.
Persisted selection is explicit owner state, not a claim of executed evidence.
Repair uses a reviewed fresh classification command before execution; malformed
receipts require operator investigation rather than automatic reinterpretation.
Requirements rebuild by repeating the typed query at the recorded catalog revision.


## Admin classification page contract

Screen: `admin.work_order_classification`, an editor for dispatch supervisors
holding `operations:dispatch:write`. A permission-gated link on the native
work-order detail opens `/admin/dispatch/work-orders/{native UUID}/classification`.
The route uses the same write permission as the classification API. The owner
query `operations.work_order_commands.classification_snapshot` returns immutable
identity/title, native kind/status, current revision, explicit classification,
owner-derived eligibility/reason, compatible purpose options, and the current
recommendation. No legacy detail-context service or mutable ORM row is passed to
this page. The read query never commits. Read and mutation share one eligibility
resolver so the browser cannot invent a second status policy.

The first viewport shows the public identifier, title, kind, status, current
classification and prerequisites. The primary action is Save classification;
Reload current work order and the detail back link are secondary navigation.
Purpose, technology and reason are the visible form fields; command UUID and
expected aware revision are hidden transport evidence. The operation identity
stays stable across validation and retry. Success redirects to the public-id
detail with a notice. The browser POST checks the matching CSRF cookie/form token
at the route boundary in addition to the application's global CSRF middleware.
The adapter parses explicit fields once and calls the typed owner executor.

Validation errors return the submitted values with escaped text and an accessible
error near the affected control. A stale revision returns HTTP 409 with all
submitted fields and the original operation/revision preserved. Saving is
blocked until the operator explicitly reloads current state; no newer revision
is silently substituted. Inactive or executed work shows the owner's disabled
reason. Unauthorized discovery is hidden and both routes enforce permission;
missing work returns 404 and malformed stored classification fails closed.

Current requirements appear below the form under Current planning
recommendations, clearly marked recommendation-only. Unclassified work has an
explicit empty state. The page never reports requirements as satisfied or assets
as activated. The layout uses one form column and expands the state summary on
larger screens; mobile retains identity, state, reason and the save/reload actions.
No totals, filters, exports, bulk operations, customer PII, or automatic external
consequences are introduced. The canonical classify audit/event remains the
administrative history owner.
