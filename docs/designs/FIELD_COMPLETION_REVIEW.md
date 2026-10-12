# Native work completion evidence and staff review

Status: candidate universal completion owner. API, web and mobile adapters use these typed owner contracts.
Real fresh/predecessor migration and PostgreSQL acceptance remain required.

`operations.field_completion` has one registry declaration and one contract. Its
primary implementation `app.services.field.transitions` retains field job
transitions; its additional declared implementation
`app.services.field.completion_review` owns the review boundary. Registry writer
coverage, liveness and module uniqueness checks cover both implementations. Typed commands publish an
explicit versioned Project or exact WorkOrder review policy, explicitly enroll a WorkOrder, publish native criteria revisions, submit immutable observations,
record a staff criterion decision or revoke a current decision. Typed current
and history queries return opaque native identifiers and immutable evidence.
No global policy or implicit approval is inferred. Reviewer independence is
explicitly configured with a nonempty reason in a separate completion-review
policy; engineering-approval policy is not reused. Independent review requires
an authoritative submitter Person binding and a freshly admitted reviewer Person;
missing submitter binding preserves evidence capture but refuses acceptance. Distinct
accounts bound to the same Person are not independent. Explicit non-independent
policy permits review without a submitter Person binding. Migration677 backfills no
submissions, verdicts or acceptance. Jobs lacking review history are
`legacy_not_reviewed`, including previously completed jobs.

The legacy field completion gate still requires configured photos and customer
signoff (or an explicit permitted unavailable reason). Its fiber as-built
requirement applies to project work orders marked `requires_as_built_evidence`,
except explicitly classified `WorkOrderAccessTechnology.RADIO` work. Unclassified
and unknown technology retain that conservative fiber gate. The shared resolver
feeds technician and vendor evidence summaries as well as completion eligibility.
An issued splice plan remains an independent obligation for every technology.
Radio completion still requires enrolled authored criterion observations and
separate configured staff acceptance before accepted project completion.

An immutable CompletionEnrollment binds the exact native WorkOrder and origin linkage snapshot. Its checked provenance is either engineering_issuance, retaining the actual migration676 issuance and exact migration675 criterion content/revision/approval/digest, or native_work_order, retaining staff-authored versioned criterion statement and verification method. Native jobs never receive fabricated engineering records. Each enrollment explicitly selects its Project or WorkOrder policy scope; legacy jobs do not infer enrollment from policy or completed status.

A submission binds its enrollment, exact immutable criteria and policy revisions, current native assignment UUID generation, exact canonical completed FieldJobEvent and authenticated technician or staff-selected native vendor member. Every criterion must appear exactly once. Technician observations contain text
and explicit existing private attachment IDs. They do not contain supervisor
pass/fail verdicts. Staff decides every criterion exactly once with an explicit
passed/failed verdict and reason; any failed criterion rejects the submission.
Criteria remain prose with the authored verification method;
this foundation does not infer numeric thresholds from prose.

Staff policy/decision/revocation requires canonical fresh active staff and Person
admission for `operations:completion-review:write`; read queries require the
separate `operations:completion-review:read`. These catalogued permissions grant
no role implicitly. Context actor must match admission. After lock waits, staff
RBAC/activity/leave and scope are revalidated before mutation. Submission uses
current canonical field actor/member admission and exact assignment generation
before new evidence or historical replay. Staff receipt replay requires current
review authority; a carried permission list alone grants nothing.

One owner executor stages immutable observations, retained evidence references,
criterion results, decisions, command receipt, mutable generation head, audit
and version-1 `field.completion_review_recorded` outbox atomically. An owner-global
review key and normalized complete intent fingerprint cover every command type;
transport correlation/command IDs are not semantic intent. Changed input under
the same key conflicts. Exact replay returns the original receipt without new
audit or event. Current queries first lock the historical Project/task scope, then engineering,
WorkOrder, policy/head; they refresh mutable state and revalidate READ after waits.
They do not present accepted history as current after WorkOrder retargeting or when
the review policy changes or engineering approval is revoked/superseded/stale.
Historical submissions and decisions remain readable without current issuance
eligibility. Revocation uses Project's typed historical engineering_plan_revocation_scope
(Project before exact optional Task) and withdraws a current decision without
requiring active origin/target eligibility or current engineering approval; it cannot restore acceptance.

Lock order begins with the review receipt advisory lock. Submission locks native
SystemUser and internal TechnicianProfile if applicable, Project then optional
Task, WorkOrder, then exact current vendor membership/native vendor as required
by canonical field access, completion policy/head, sorted physical object-key
advisory locks, StoredFile aliases, and attachments. It never locks vendor
membership ahead of WorkOrder. Staff decisions lock Project/Task, then the
engineering owner's distinct typed completion participant plan/target locks,
WorkOrder, completion policy/head. Both paths hold Project before these different
suborders. The participant checks exact issued approval/revision/digest and
binding freshness, with completion review admission, and skips new-issuance-only
on-hold/blocked/done eligibility. Submission locks the typed historical exact Project/task scope and preserves
observations and authorized receipt replay even after origin archival or
approval revocation; only acceptance depends on current approval. No private
engineering helper or dispatch issuance participant is called.

`files.evidence_retention` owns the immutable `StoredFileRetention` ledger and
typed flush-only registration by the named active completion owner. Storage does
not import or interpret completion decisions. Completion writes its own immutable
attachment/checksum references and registers retention atomically. Common storage
locking serializes provider/physical-key and all aliases before submission or
canonical deletion. Before registering retention, the storage participant streams
the bounded physical object and verifies exact SHA256 and byte size; absent or
inconsistent bytes cannot become retained evidence. Provider failures remain typed
retryable errors. Both hard-object deletion and staged soft deletion refresh
stale ORM input and refuse any retained physical alias before changing metadata
or calling S3. Attachment removal refuses retained file references under the same
file lock. Storage never acquires WorkOrder. This gives one direction in the
registry graph: completion depends on storage retention, with no reverse edge.
Existing non-retained hard deletion remains an external effect preceding its
metadata commit and therefore retains its pre-existing rollback limitation.
There is no retention-expiry or deletion override in this foundation. Physical deletion also preserves other active object aliases before retention exists. All canonical metadata creation holds the same object-key lock. Direct upload locks before PUT; prepared upload staging verifies the object still exists under lock and returns a typed retryable missing/unavailable error before creating active metadata. Prepare itself stays outside database locks; an object removed between preparation and staging requires a fresh prepare/retry. Unreferenced prepared objects remain an existing cleanup concern, and cleanup must respect retained references.

PostgreSQL RESTRICT FKs, immutable triggers, deferred exact-criterion completeness
checks and child-creation-only guards protect evidence and decisions. Immutable
head lineage and generation checks prevent accidental pairing across submissions.
Migration677 uses five-second lock and 120-second statement budgets; retry the
whole migration on contention. Empty additive DDL uses IF NOT EXISTS for the
existing squashed fresh baseline's model-precreated tables only after fail-closed
adoption verifies frozen PostgreSQL column, structural constraint and index
shapes. The contract is pinned from fresh/predecessor comparisons; unexpected
shape or existing history refuses adoption before DDL. Downgrade refuses history destruction;
retain evidence and forward-fix.

The support lifecycle may explicitly request and confirm resolution against an exact native Ticket completion basis. The completion participant requires the named active support.ticket_lifecycle owner and uses only WorkOrder.origin_ticket_id. It returns exact work_order/enrollment/criteria/policy/assignment/completed-event/submission/accepted-decision/review-generation tokens. A new criterion, policy, submission, assignment, completed outcome or linkage drift invalidates the basis. Linked-enrollment presence is a separate narrow read under the linkage fence and grants no acceptance authority. Direct closure of an enrolled linked job must use the support confirmation flow; no completion command closes a Ticket.

Enrollment takes the canonical native linkage advisory fence before Ticket, Project/Task and WorkOrder locks. The WorkOrder-owned typed admission checks the exact origin Ticket is active and nonterminal, and returns the native linkage snapshot. Canonical native creation and support closure share the same fence: close winning prevents enrollment, enrollment winning prevents direct closure. Review and history never lock Ticket. For multiple support basis entries, all Project roots lock in sorted order before Tasks, engineering participants, WorkOrders, criteria/policy and review heads.

Acceptance does not change Project/task status,
InstallationProject verification, provisioning release, payment or finance.
Field execution completion is a separate observation. No host/database mutation,
Docker, publication or deployment is part of this source slice.

`tests/test_field_completion_review.py` is explicitly fast SQLite behavior,
including exact criterion sets, independent review, stale engineering, replay
authorization, rollback and physical-object alias retention. Architecture guards
protect the ownership graph and effect ordering. The authored
`tests/integration/test_field_completion_review_acceptance.py` uses canonical
issuance, assignment, native staff admission and migrated PostgreSQL clones for
concurrent receipt, immutable evidence, retained aliases and rollback checks.
It must run on the dedicated test server; source and SQLite results establish no
migrated-schema or concurrency acceptance claim.

## Protected adapter reads

`policy_snapshot` provides protected policy discovery. Publishing a policy and
enrolling a work order are separate explicit commands. Enrollment binds the exact
native work order to that policy and either authored native criteria or its actual
approved engineering criteria; policy publication alone does not enroll jobs.
`workspace` discovers the native enrollment by public WorkOrder identifier and
returns its exact criterion prose/method, policy and assignment/head generation
tokens, current submission/decision, and domain-owned `ActionReadiness`. READ
purpose includes immutable history. Separate WRITE decide/revoke purposes require
fresh canonical staff/Person/leave admission without a READ floor and expose only
the exact current submission/decision, never broader history. A revoke preview
does not depend on current engineering approval. Previews never mutate and do not
replace command-time validation. Namespaced action keys are `field_completion.*`.

`field_submission_preview` requires the exact current native assigned actor/member
before enrollment discovery and revalidates after Project/WorkOrder/policy/head waits.
It exposes immutable enrolled criteria, the current policy revision and exact native
assignment/head tokens without a completion READ permission or current engineering
approval gate. Its review state reports the last recorded staff verdict; it does
not assert fresh engineering acceptance. Missing enrollment/policy/assignment and
retargeting are explicit readiness blockers. Commands optionally bind the expected
public WorkOrder route identifier before replay and mutation; transport adapters
supply this correlation when their URL uses a public WorkOrder identifier.

`evidence_content` grants no general private-file permission: fresh staff READ
access is restricted to exact immutable enrollment/criteria/submission/attachment lineage, including
actual engineering issuance only for engineering-enrolled work;
WRITE decision purpose can access only the exact current pending submission.
Historical evidence remains readable after acceptance/revocation and origin
archival. Storage's typed `stream_retained_evidence` participant validates the
retention ledger and immutable checksum and buffers only the canonical bounded
attachment size, verifying bytes before any response content is returned. Staff
admission is revalidated after locks and provider access. No storage key or raw
object URL is returned to adapters.

The protected workspace returns purpose-specific ActionReadiness for enrollment, policy publication, native criteria publication, decision and withdrawal. WRITE-purpose reads do not impose an unconditional READ floor; READ history remains independently authorized. Field preview uses current exact assigned actor admission before enrollment/history discovery and returns a blocked200 for unenrolled work. Original enrollment/criteria/policy/assignment/completed-event tokens are preserved through offline drafts. Historical submission records include their exact immutable criterion text and method, never current labels.

## Reviewed failed-scope replacement (migration679)

`operations.field_completion` also owns `review_failed_scope_replacement` and
`revoke_failed_scope_replacement`. A supervisor reviews a one-to-one coverage
mapping, never waives acceptance and never rewrites the failed field event or
work-order status. The source must carry the exact latest authoritative
`unable_to_complete` event and assignment generation, or the exact current
rejected submission, decision and review generation. Native enrollments require
the same non-null origin ticket and subscriber and equal project/task identities.
Engineering enrollments instead use their exact project/task scope, permitting
subscriber-free infrastructure work. The handoff owner's protected typed receipt
query proves exact receipt-to-enrollment/work-order identity. Both receipts must
name the same actual plan, revision, approval, digest, infrastructure target,
purpose and technology. Both current approval checks use the engineering owner's
completion participant. Pinned canonical criteria must be identical, with identity
coverage mapping. Mixed provenance, changed engineering scope or a newly approved
revision refuses; this is not an implicit engineering scope-change approval.

The immutable decision freezes both current criteria and policy revisions,
both assignment generations, complete explicit source-criterion coverage,
fresh staff admission, reason and actor Person. Each policy independently
controls reviewer independence for its affected scope; current assignees,
field-event authors and submission authors are checked through canonical Person
bindings. There is no implicit approval bypass. One active successor and one
active predecessor permit A → B → C, while cycles, fan-in and branching refuse.
Revocation appends history and advances the source head. New review after
revocation requires a new exact generation and fresh facts.

The command takes the owner-global receipt lock and the canonical linkage fence
before Ticket (native scope) or the exact historical Project/task engineering
scope, then sorted engineering/WorkOrder and criteria/policy/review heads and
replacement heads. Engineering graph decisions use the project revocation scope;
new enrollment and replacement share linkage-before-project ordering. Existing
issued scope can be reviewed while the project is held or task blocked/done;
creating a new visit still uses normal issuance eligibility. Historical reads and
explicit revocation do not require current engineering approval or an open ticket. One owner executor atomically commits immutable
coverage, decision/head, receipt, audit and outbox, or rolls everything back.
Exact receipt replay freshly revalidates staff admission and route scope and
emits no second audit/event. The lineage fence serializes absent-head races and
support confirmation/grace timers. The protected workspace query is the only
source of failure tokens, same-ticket eligible candidate labels, criterion
prose, policy independence, current review state and action readiness.

Every enrolled original ticket scope remains in `TicketCompletionBasisEntry`.
An entry preserves its original work-order/enrollment identities and separately
names its effective accepted endpoint and ordered replacement decision IDs.
Every traversed decision revalidates failure, scope, assignments, policies,
criteria and coverage; the endpoint still needs independent completion
acceptance. Reassignment, criteria/policy changes, evidence drift or revocation
invalidates the basis. Support stores immutable effective endpoint and ordered
lineage child rows with each confirmation request. Legacy unenrolled work
retains its existing behavior; nothing auto-completes a project/task, restores
an outage, charges money or closes the ticket.

Migration679 is additive with no backfill. Empty metadata-precreated tables
must match frozen model-independent columns, constraints and indexes; populated
or altered pre-migration tables refuse adoption. Real PostgreSQL fresh-baseline
and 678-to-679 rehearsals and concurrency acceptance remain required.


`current_replacement_evidence` is a protected typed read for an exact failure and
current approved replacement decision. It requires fresh completion WRITE staff
admission, locks and revalidates the same graph, and returns immutable proof; it
never creates a replacement, waives acceptance or changes a ticket. Other owners
must invoke it before taking their own work-order locks.
