# Field lifecycle delivery and acceptance

Status: integrated source capability map, 2026-10-11. This document describes
implemented boundaries and the remaining user workflow. It does not certify a
production deployment, migration rehearsal, notification delivery or field pilot.
The executable SOT registry remains authoritative for ownership. Existing domain
designs linked below retain their detailed contracts.

## Intended operator and customer journey

A complaint or service request starts with the accountable intake owner. An
agent should have short paths to find the customer, identify an existing ticket
or shared incident, capture the problem and location, and issue the next reviewed
field scope. Those shortcuts must call existing owners and preserve their
permissions, provenance and idempotency. A tag, classifier suggestion or freeform
“install” string is never a dispatch command. The integrated work does not add a
unified issue-intake wizard or automatic assignment planner. Ticket detail now
links to explicit incident correlation, dispatch and accountable failed-visit
follow-up; those actions retain their separate owner approvals.

Support records and triages complaints through `support.ticket_lifecycle`.
An active member of the assigned service team explicitly issues field work
through `support.ticket_work_order_handoff`, with the required support and
dispatch permissions. One ticket can require zero, one or many visits. The native
`WorkOrder.origin_ticket_id` is the relationship; neither imported provenance nor a
copied ticket metadata identifier substitutes for it. A linked ProjectTask may
supply reviewed project scope. Native project/task lifecycle belongs to
`operations.project_lifecycle`; work-order creation and relationship writes
belong to `operations.work_order_commands`.

The supervisor reviews scope, site/access instructions, priority, schedule,
skills, safety and material needs before explicit assignment. Creation does not
select a technician from project assignees. The existing assignment command
selects a native technician or an explicit active attending vendor member and records an assignment
generation. Rescheduling preserves that generation; an explicit assignment,
including a repeated assignment to the same technician, rotates it. The assigned
actor accepts the visit; that acceptance is different from a supervisor accepting
completed work.

A technician assignment can disclose the exact current active SystemUser's
independently reviewed name, company and portrait through
`customer.field_identity`. An organisation assignment is not an actual crew
selection: vendor-owner, neutral person and imported identifiers must never be shown
as the person who will attend. Vendor execution requires the explicitly selected
active native member. Customer disclosure uses that person's independently
reviewed identity. Legacy company-only assignments return
`actual_crew_not_selected` until a supervisor explicitly selects an attending person.

Assignment, reassignment and schedule changes through the typed assignment
command stage customer communication intents transactionally. Channel selection,
suppression and delivery belong to `communications.intents`. An intent is not
proof of delivery. Subscriber-free infrastructure visits do not fabricate a
customer recipient. Customer tracking displays owner-provided schedule/estimated
arrival when available; an ETA is an estimate, not a promise or an invented
arrival time. Current-generation departure evidence, consented sharing and a
fresh job-tagged fix govern live tracking. The reviewed identity/photo is fetched
through a scoped, private, no-store read rather than placed in a public message.

Customer visit chat uses `customer.field_job_chat` and
`communications.team_inbox_field_job`, with inbound admission owned by
`communications.team_inbox_commands`. Availability and send eligibility come
from those owners. The write locks and rechecks subscriber, visit and conversation
scope; completion and message admission serialize against the same visit.
Closed-history reading does not imply permission to send. The customer app
retains failed drafts and requires an explicit retry after an uncertain send;
the optional request UUID provides exact replay through the canonical inbox
owner. The app retains that UUID for explicit retries of an unchanged draft,
and rotates it after a successful send or draft edit.

## Shared infrastructure and planned work

The intended shared-outage journey is many customer complaints → one network
incident → one canonical infrastructure ticket → as many explicitly issued
field scopes as needed. Each customer complaint remains a support case with its
own response and confirmation; neither one visit per customer nor automatic
closure of all complaints is implied.

Implemented: `network.outage_lifecycle` owns incident transitions and immutable
audience revisions; `network.outage_impact` owns impact resolution.
`network.outage_work_order_handoff` creates/binds the canonical infrastructure
ticket and issues infrastructure work orders, preserving the issued target,
audience revision and membership token. Different reviewed commands can issue
multiple visits. A stale audience or conflicting retry refuses issuance.
`operations.field_jobs` exposes the linked job's issued/current audience through
a bounded read-only brief. Counts describe affected subscriptions, not confirmed
downtime or deduplicated people. Current revision changes do not erase issued
provenance or redirect the visit silently.

Reviewed complaint correlation is available from ticket detail. The agent selects
the exact native customer service from the persisted incident audience and records
a reason. Original-service proof and immutable receipts prevent a later audience
change from silently rebinding that complaint. This association does not merge
complaints or grant mass-closure authority. Shared acceptance gating remains a separate contract. Each linked complaint's
resolution request pins the exact selected service, incident binding, current
persisted audience and restoration decision. Every actually issued scope whose
frozen audience includes that service must have a current accepted basis or an
exact reviewed replacement. An explicit restored-without-field-scope disposition
is available only when no matching issued scope exists. Confirmation and grace
closure revalidate this complete evidence; a new issuance or changed binding
requires a fresh request. Unknown legacy correlations require an explicit staff
review that creates new service proof while preserving their original history.
Neither incident association nor field acceptance establishes restoration.

For planned rollout, the project/task owner supplies the approved plan and native
relationships. `operations.project_engineering_plans` records immutable plan
criteria, approval decisions and a versioned, configurable reviewer-independence
policy. Approval freezes the exact project/task and infrastructure scope;
changing that scope requires a fresh approval. The project handoff can issue
physical work only from the current approved plan. It refuses new issuance while
the project is on hold or the task is blocked or done. Those issuance rules do
not prevent reading history, recording completion evidence or revoking a review.
Subscriber-free infrastructure work does not invent a customer or complaint.

Dispatch explicitly classifies the existing draft/scheduled work
order through `operations.work_order_commands.classify`:

| Native kind | Explicit purpose | Access technology |
| --- | --- | --- |
| `infrastructure` | `infrastructure_rollout` | `fiber` or `radio` |
| `customer` | `customer_deployment` | `fiber` or `radio` |
| Either kind | `repair`, `maintenance`, `upgrade`, `relocation` | `fiber` or `radio` |
| Either kind | `repair`, `maintenance` diagnosis | `unknown` |

The current admin detail link opens a separate classification form; the typed
API is also available. Create-and-classify is not one command or one form.
Historical rows stay unclassified. No purpose or technology is inferred from
legacy work types, titles, tags or project labels.

`operations.work_order_templates` owns the versioned recommendation catalog.
Fiber preparation includes route/core/port/splice plans and applicable optical,
continuity and OTDR evidence. Radio preparation includes line of sight,
spectrum/link design, safe mounting/grounding/power and applicable alignment,
interference and service tests. Approved engineering/site plans own acceptance
criteria; this document invents no thresholds. Recommended safety/permit,
material, test, as-built and handover gates are **not an enforced checklist**.
The field brief/mobile suggested-preparation section is a read projection.
Recorded-version drift withholds recommendations or rejects historical replay;
it does not silently reinterpret an earlier classification.

## Execution, evidence and acceptance

`operations.field_work_order_access` admits the current assigned native actor;
`operations.field_completion` owns field transitions and completion evidence.
The existing visit path includes acceptance, departure, arrival, start,
pause/hold, resume, complete and unable-to-complete. Current assignment generation
is required for generated-assignment observations and transitions. The job brief
shows the exact linked active ProjectTask and instructions, without granting
access to sibling tasks or completing the task on a read.

Existing completion policy is supplied by the server. With evidence enabled,
it requires a photo and customer signature or an allowed signature-unavailable
reason. The existing project-scoped `requires_as_built_evidence` policy also
checks exact work-order fiber evidence. These are bounded existing rules;
they do not validate every recommended fiber/radio plan, optical threshold,
permit, handover item or installed asset. A signature-unavailable reason is
recorded evidence of the exception, not fabricated customer acceptance.

Execution evidence and supervisor acceptance are separate decisions owned by
`operations.field_completion`. Explicit enrollment supports both native ticket
work orders and work issued from an approved engineering plan. A supervisor
configures the versioned independence policy and authors native acceptance
criteria, or uses the exact issued engineering criteria. Enrollment does not
invent thresholds or silently enroll historical work. Submission pins its
criteria, policy, assignment generation and exact completed event. Changed
criteria, policy, assignment or completion evidence invalidates current
acceptance; historical evidence retains its original labels and receipt lineage.

Field outcomes reach the origin ticket through the receipted handoff consumer
and official timeline. For tickets with enrolled work, support refuses direct
closure and requires a resolution request containing the full current accepted
basis for every enrolled origin work order. Customer confirmation and the exact
request-bound grace timer revalidate that basis. A changed basis needs a fresh
request; expired or superseded links cannot act. A current customer dispute can
reopen the ticket without requiring that the technical basis remain accepted.
Unenrolled legacy tickets retain their existing support behavior. A completion,
signature or rating never directly closes the complaint or project task.

For project-bound enrolled work, an immutable requirement marker prevents generic
project/task completion or archival from bypassing accepted work. Staff explicitly
apply the full current accepted field basis to the selected task subtree or
project. The single field owner validates every registered requirement and reviewed
replacement, then stages completion through the protected project owner. Exact
replay revalidates current evidence; revoked acceptance or newly enrolled scope
requires a new reviewed action. Existing terminal history and unenrolled legacy
behavior remain intact.

`network.outage_lifecycle` separately verifies/observes restoration under its
existing state machine. Its recovery observation loop can resolve an incident
under existing policy, but never closes tickets or work orders. Restoration remains an explicit network policy decision; field acceptance does
not independently establish restored service. `operations.service_order_lifecycle`,
`operations.provisioning_workflow`, `operations.provisioning_lifecycle` and
`access.subscription_lifecycle` retain service release, provisioning and
activation decisions. Classification, installation photos and work-order
completion do not automatically activate assets, provision a service or begin
billing. Billing owners retain their own authorized consequences.

## Costs, materials and workforce

Expenses are submitted against an eligible authoritative work order through
`operations.expense_requests`; `operations.expense_categories` supplies ERP
category rules. The selected eligible independent approver decides in Sub.
ERP owns reimbursement destination verification, payment execution and the paid
fact. A completed visit does not approve or pay a claim.

`operations.material_dependencies` owns contextual requests and observed
fulfillment state; `operations.material_catalog` supplies eligible ERP item
projections; `operations.material_consumption` owns allocation-capped consumption
evidence. ERP owns stock, serials, issuance and refusal. Do not treat a template
bill of materials, submitted request or offline consumption draft as issued
stock. Manual and ERP request channels and their configured cutover behavior
remain distinct; generic field completion adds no approval or ERP capability.

Attendance uses the existing workforce read/punch capabilities. ERP owns employee
mapping, shift/geofence decisions, attendance ledger and hours. Attendance punches
are online-only; a location fix, job arrival or expense activity mark is not a
check-in. Confirmed attendance and location sharing remain separately visible
when either operation fails. Vendor execution does not imply an ERP employee
mapping or a vendor attendance capability.

## Failure, privacy and automation boundaries

Vendor location access is limited to the exact destinations and explicitly
related assets of their currently authorized work order. Nearby assets and the
global network map are not vendor destinations. Vendor execution does not
request GPS tracking or send location pings; staff tracking remains a separate
permission and consent-controlled capability. Opening directions uses the
current authorized destination, rather than a stale cached map selection.

| Case | Required behavior and current boundary |
| --- | --- |
| No access, unsafe site, missing materials or failed test | Pause/hold or unable-to-complete with the required reason and retain evidence. The failed-visit workspace surfaces durable unable/rejected outcomes without an obligation. A supervisor explicitly records responsible native team, optional member, due time and triage/revisit/no-revisit disposition. A replacement must have an exact approved coverage decision; notes, a copied ticket or a new visit alone do not discharge failed scope. Scheduling and escalation remain explicit owner actions. |
| Reschedule or reassignment | Use the assignment owner. Preserve reschedule lineage, rotate explicit assignment generation, stage the appropriate customer intent and refuse old-generation execution/location evidence. Legacy header schedule edits do not acquire parallel notifications. |
| Offline capture | Supported field outboxes retain their captured job/generation and delivery state; never restamp an old observation onto a new assignment. Queued/failed notes remain visible. A queued action is not accepted server evidence. Attendance and customer chat are not silently queued for replay. |
| Stale planning form | Preserve values and original operation/revision; return a conflict and require explicit reload. Classification locks/rechecks before writing and stages audit/event atomically. |
| Delivery or ERP failure | Show pending/failed owner outcome and retry only through the durable delivery owner with stable keys. Do not infer notified, issued, paid or provisioned from an attempted transport. |
| Identity unavailable or revoked | Withhold card/portrait; recheck current assignment, active native identity and reviewed revision. Never fall back to raw KYC, a public avatar, vendor owner or cached verification claim. Delivered images cannot be recalled. |
| Tracking privacy | Exact customer/job/current-generation scope, enabled sharing and fresh departure evidence govern disclosure. Another job, stale fix, terminal job or superseded assignment must not expose coordinates. |
| Shared-incident audience drift | Keep issued provenance, label current persisted revision, reject inconsistent pagination and restart the read. No live topology recomputation or customer list copied into the work order. |

Safe automation already includes authorized owner-output delivery, exact retry
receipts, configured reminders, existing observation loops and read projections.
Automation may suggest scope or surface missing information. Explicit approval
remains necessary for field issuance/assignment, reviewed identity, engineering
acceptance and the relevant expense/material/service decisions. Existing approved
owner policies may automate their own consequences; this document grants no new
blanket approval. In particular no classification event triggers dispatch,
provisioning, billing, asset activation or ticket closure.

## Acceptance matrix and remaining delivery gates

“Implemented” below means source capability, not completed deployment acceptance.

| Capability | Current evidence/owner | Acceptance or gap |
| --- | --- | --- |
| Complaint → reasoned native visits | Ticket handoff and origin FK | Implemented; permissions, team membership, retries and multiple visits covered by handoff tests. Unified intake wizard remains a usability opportunity; existing ticket detail exposes owner actions. |
| Shared incident → infrastructure ticket/visits | Outage lifecycle/handoff | Reviewed selected-service correlation and shared-scope completion gates implemented; each complaint retains its own current evidence and confirmation. Restoration remains independently owned. |
| Supervisor dispatch and visit lineage | Work-order assignment owner | Implemented; typed command rechecks/atomic customer intent; production notification delivery must be observed separately. |
| Technician identity/photo | Field identity review + customer disclosure | Implemented; independent review, current assignment/revision, private file access. No real identity collection or approval implied by delivery. |
| Vendor execution and actual attending crew | Native field actor/access; explicit unavailable identity | Explicit member selection, exact-person execution and independently reviewed customer disclosure implemented; operational pilot remains required. |
| Customer schedule/ETA/tracking/chat | Customer self-care, tracking and inbox owners | Implemented with availability/privacy gates; missing ETA remains unavailable; uncertain chat send needs explicit review/retry. |
| Fiber rollout/customer fiber or radio deployment | Engineering plan approval, project handoff, explicit classification/catalog + job brief | Versioned configurable approval and exact approved issuance implemented; site-specific acceptance criteria remain operator-authored. Generic catalog recommendations do not invent test thresholds. |
| Task/evidence/signature | Project/task owner, field completion/notes | Explicit native/engineering enrollment, authored criteria and configurable supervisor acceptance implemented. Project/task completion requires an explicit full-basis action for registered scopes. |
| Ticket result/customer confirmation | Handoff output + support confirmation | Enrolled origin work requires current accepted basis, immutable resolution request and confirmation/timer revalidation; field outcome never directly closes ticket. |
| Restoration/activation/billing | Existing network/service/access/financial owners | Separate decisions preserved; no new field-completion automation. |
| Expenses/materials/attendance | Existing field/ERP boundaries | Existing capabilities; live connector binding, ERP outcomes and exact requester scope require operational acceptance. |
| Failure/revisit planning | Exact failure evidence, reviewed replacement coverage and accountable follow-up | Explicit native/engineering scope replacement and immutable responsible-team/person/due/disposition history implemented. A revisit remains pending until separately approved and issued; no automatic assignment or fabricated appointment. |
| Offline and reassignment races | Generation-aware transitions/location, note outbox, scoped reads | Focused unit/mobile evidence; PostgreSQL lock/concurrency and real-device offline acceptance remain required. |
| Release readiness | Additive lifecycle migrations and integrated contract/UI tests | Dedicated test-server fresh/predecessor migrations, Linux full suite, browser/mobile review and end-to-end pilot are release gates. Local SQLite tests do not prove them. Google Play account identity and phone verification previously prevented app-record creation; current account status has not been reverified. The current source check still refuses the missing Field Google Play app-signing SHA-256 in assetlinks.json. |

The next usability slices are a unified intake wizard, scheduling/escalation
assistance and technology-specific execution guidance beyond explicitly authored
acceptance criteria. Each needs its own typed authority, authorization,
audit/idempotency and acceptance criteria before clients can present it as done.

## Source contracts

- [Ticket handoff](TICKET_WORK_ORDER_HANDOFF_SOT.md), [outage handoff](OUTAGE_WORK_ORDER_HANDOFF_SOT.md), [outage lifecycle](NETWORK_OUTAGE_RESPONSE_LIFECYCLE.md).
- [Project/task ownership](PROJECTS_SOT_COMPLETION.md), [classification catalog/admin page](WORK_ORDER_TEMPLATE_CATALOG.md), [assignment updates](WORK_ORDER_ASSIGNMENT_CUSTOMER_UPDATES.md), [field job brief](FIELD_JOB_BRIEF.md).
- [Identity review](FIELD_IDENTITY_REVIEW.md), [customer identity](CUSTOMER_FIELD_IDENTITY.md), [customer conversation](CUSTOMER_FIELD_VISIT_CHAT.md), [message admission](CUSTOMER_FIELD_CHAT_ADMISSION.md).
- [Field notes](FIELD_WORK_ORDER_NOTES.md), [expenses](WORK_ORDER_EXPENSE_ENTRY.md), [materials/vendor/ERP](MATERIALS_VENDOR_ERP_CHAIN.md), [attendance](WORKFORCE_ATTENDANCE_INTEGRATION.md).
- [Completion review](FIELD_COMPLETION_REVIEW.md), [accepted project completion](ACCEPTED_PROJECT_FIELD_COMPLETION.md), [support confirmation gate](SUPPORT_COMPLETION_CONFIRMATION_GATE.md), [accountable failed-visit follow-up](FIELD_VISIT_FOLLOWUP.md).
- [Executable ownership map](../SOT_RELATIONSHIP_MAP.md); field completion implementation: `app/services/field/transitions.py`.

## Transaction provenance

Immutable completion children must be created in their parent's physical outer
transaction. PostgreSQL stamps a full `xid8` identifier on parent insertion and
refuses changes to that marker. Nested and released savepoints retain the same
outer identity; a later committed transaction cannot append evidence. Existing
parents keep an unknown marker and receive no invented creation provenance.
Fresh metadata adoption permits only the explicitly named nullable native
columns without defaults; all other frozen schema checks remain mandatory.

## Physical project entry

The project owner and staff infrastructure picker share one explicit eligibility
policy for cable rerun, fiber/radio relocation, fiber/radio installation and
cross-connect projects. Customerless rollout uses a native surveyed site or asset
anchor. Typed fiber and radio entry acceptance exercises plan approval, issuance,
field acceptance and explicit project completion. Existing target locks and vendor
assignment restrictions still apply; project type does not invent a technology
commissioning threshold or bypass authored acceptance criteria.

Explicit radio work uses the same configured photo/sign-off gate and authored
criterion review without requiring unrelated fiber records. Fiber and unknown or
unclassified work retain the conservative fiber check. Declared splice-plan
obligations remain independent. The staff browser fixture is explicitly synthetic;
its relaxed evidence policy does not establish physical commissioning acceptance.

The migration graph preserves the lifecycle revision IDs and joins the customer
connection-type history at revision 683. Revision 684 converges connection-type
server defaults found to differ between fresh metadata bootstraps and real
predecessor upgrades. It changes defaults only; existing customer assignments
and definition values are preserved. Release validation compares the fresh
schema with upgrades from current main and prior lifecycle revisions.

The assignment-generation migration uses filename `685_assignment_generation.py`
while retaining revision `671_assignment_generation`, parent
`670_user_credential_version`, and its original migration body. The filename
relocation avoids the published customer-connection migration's numeric prefix
without replaying already-applied database history. This follows the preserved
revision convention documented for prepaid purchase migrations. Merge admission
for this exact preserved history is governed by accepted ADR 0030, explicitly
approved by Michael on 2026-10-12. Its guarded owner implementation and final
validation remain required before publication.


The accepted router-health convergence amendment adds inert revision 686 after
Field 684 and published router-health 672. It preserves prior migration IDs,
parents and bytes; the field-identity migration filename is 687 to avoid a
numeric filename collision. Admission pins the exact reviewed main commit and
requires the amendment's separate acceptance. Fresh, actual main-672 and
Field-684 upgrade paths require equal affected schemas and preserved predecessor
rows before acceptance. Published router-health server defaults are also
represented in ORM metadata so fresh bootstrap has the same defaults.
