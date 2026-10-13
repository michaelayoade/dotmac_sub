# Native project engineering plan approval

Status: implemented native engineering-plan authority. Approved field issuance
is described in PROJECT_INFRASTRUCTURE_HANDOFF.md; protected staff adapters are
described in PROJECT_ENGINEERING_STAFF_ADAPTERS.md. Deployment acceptance remains
a separate release gate.

`operations.project_engineering_plans` owns native immutable plan revisions,
ordered criteria, append-only approval/rejection/revocation/supersession decisions,
per-Project versioned approval policy, generation heads and historical receipts.
`operations.project_lifecycle` supplies typed Project/task identity, current
eligibility, engineering bindings and preview timestamps. Its origin participant
reuses `_lock_project_task_scope`, preserving Project-before-task locking and
inactive/mismatched/superseded-task denial.

The initial scope is exactly the existing structural `ProjectInfrastructure`
target, resolved through `InfrastructureReference` and the native catalogue.
The Project owner's explicit physical-type policy makes this selection available
for installation, relocation, cable rerun and cross-connect projects. Typed
Project creation is the entry path for customer fiber/radio deployment and
customerless rollout phases; an existing native site/asset anchors each planned
scope. No customer or commissioned asset is invented by planning.
Fiber/radio is explicit. Location is either a native POP-site UUID or a reviewed
survey description plus bounded finite WGS84 Decimal latitude/longitude. Scope
text and one to 100 ordered immutable criteria carry operator-supplied statements
and verification methods. Criterion UUIDs and order values are unique per
revision. No title/tag inference, invented numerical acceptance thresholds,
greenfield-route/multisite support or file UUID acceptance is provided. File
uploads and evidence retrieval require a separately protected file entity and
staff reader; generic broadly readable StoredFile records are not accepted.

The typed service offers `publish_policy`, `draft_revision`, `decide` and
`resolve_current`; the protected HTTP/admin adapters are documented in
[the staff adapter contract](PROJECT_ENGINEERING_STAFF_ADAPTERS.md). Commands accept canonical
server-produced `PermissionAdmission`, `CommandContext`, expected head generation,
exact native origin and conservative Project/task `updated_at` preview tokens.
Those timestamps are not lifecycle counters. Long-lived approval freshness uses
exact project/task/template bindings and infrastructure target plus immutable
content digest; unrelated title edits do not invalidate approval.

Approval policy has no global Boolean or bootstrap fallback. A privileged
`project:engineering_plan:policy` command publishes an explicit per-Project
revision with `independent_reviewer_required` chosen by the operator and a
nonempty business reason. Publishing policy never approves a plan. An approval
selects the exact current project policy revision and freezes its identity.
Later policy changes do not rewrite historical approvals. No policy means no
approval. Admin follows canonical RBAC semantics but never bypasses the selected
reviewer-independence rule. Author/reviewer comparison uses canonical active
Person UUIDs rather than authentication principal UUIDs. Missing, inactive or
changed author Person binding fails closed at review.

Permissions are `project:engineering_plan:read`, `:write`, `:approve`, `:revoke`
and `:policy`. Migration675 adds the permission catalogue entries and grants no
roles or users. The new `auth.staff_permission_admission` owner performs strict
fresh staff and Person admission through `staff_permission_admission`. It delegates
the existing gate-owned canonical RBAC calculator in `permission_authorization`;
HTTP dependencies serialize legacy HTTP auth mappings into that same typed
`PermissionPrincipal` calculation (native UUID, principal-kind enum, role/scope sets).
This capability owns no generic route gate, grants, authentication mutation or
denial audit. Existing `auth.permission_gate` remains in its legacy baseline:
its request transport and leave-denial audit/commit debt are outside this slice;
no read-only claim or full migration is made for that module. Existing permission aliases,
wildcards, direct/role permissions and admin semantics remain intact. Current
staff/principal activity, native vendor exclusion, canonical active Person and
write leave restriction are rechecked before every mutation and receipt replay.
A final fresh authorization check after domain lock waits is the authorization
linearization point; the canonical Person must equal the initially admitted
Person. Authorization changes after that point govern subsequent commands.
Waited receipt replay also rechecks authorization before returning. Protected
current reads recheck read authorization after their lock waits.
Read resolution requires current read authorization. No client permission list
can replace canonical server revalidation.

Each mutation enters `execute_owner_command` once and stages receipt, audit and
versioned `project_engineering_plan.changed` outbox event atomically. Lock order
is Project, optional task, plan head, policy head for decisions, then inventory
and explicit POP-site location in stable native-table/UUID order. PON scope
also locks and revalidates its parent OLT. The exact parent OLT UUID is frozen
in the immutable origin binding and approval digest; later PON reparenting
invalidates current approval and requires a new revision and review. Policy publication locks Project then
policy head. Mutable inventory targets are revalidated under locks. Deadlocks or
serialization failures require whole-command retry; stale origin/head/policy,
authorization and conflicting idempotency keys are final refusals.

One plan family exists per Project/optional task/technology, enforced by partial
unique indexes. Drafting a new immutable revision advances the plan generation
without changing the current approval. Only the latest unreviewed revision may
be approved or rejected. Rejection requires a new revision before another
review. Approval appends an explicit supersession decision for the former
approval and installs the new approval. Revocation clears the current approval
without falling back to older approvals. A separate typed historical Project/task
participant locks the exact origin without requiring active infrastructure or
current template eligibility, so revocation remains possible after archival,
supersession, infrastructure removal or changed engineering bindings. Review and revocation record exact content digest,
policy revision, actor/author Person snapshots and reason.

Owner-global idempotency keys fingerprint actor/reason and all decision-bearing
inputs. Exact same-key retry returns the original historical typed receipt only
after fresh authorization, even after later revocation; it neither reapproves nor
emits another event. Conflicting input under that key is denied. A new key must
pass all current checks. Current resolution locks Project/task/head for a
coherent snapshot, requires the exact current approval/revision/digest and
unchanged engineering bindings, and fails closed for archived/superseded scope,
Project `on_hold`/`completed`/`canceled`, or task `blocked`/`done`/`canceled`.
Drafts/reviews may remain available while work is held; this eligibility gate
protects future issuance and confers no issuance authority itself.

Migration675 adds native tables and the exact task/project composite key. Native
foreign keys preserve plan/revision/decision pairing. PostgreSQL triggers reject
updates/deletes of immutable evidence, forbid plan binding changes/deletion, and
allow criterion inserts only in the revision's creation transaction. Application
ORM guards also reject evidence edits. No approval or policy is backfilled.
Downgrade refuses to destroy history; retain additive schema and forward-fix.
Deployers use the migration's 5-second lock and 120-second statement budgets.

Focused SQLite unit tests are explicitly non-authoritative behavior evidence.
Before adoption, real migrated PostgreSQL must prove fresh and 674-to-675
upgrades, composite FKs/partial uniqueness/immutable triggers, concurrent first
family/key creation, approval/policy supersession races, locked inventory/archive
races, atomic receipt/audit/outbox rollback, and durable exact receipt replay.
No local Docker, test-server mutation, deployment or PostgreSQL acceptance is
part of this source slice. `PROJECT_INFRASTRUCTURE_HANDOFF.md` describes the separate candidate
issuance coordinator and its pending PostgreSQL acceptance contract; subscriber/ticket synthesis, assignment,
notification, closure, restoration, provisioning and finance remain outside it.

The typed `stage_current_for_issuance` participant is available only within the
active project handoff owner transaction. It requires hardwired fresh dispatch
permission and resolves exact current approval through the same private freshness
implementation. Ordinary `resolve_current` retains engineering READ admission;
the participant grants no plan mutation, draft access or historical list access.
