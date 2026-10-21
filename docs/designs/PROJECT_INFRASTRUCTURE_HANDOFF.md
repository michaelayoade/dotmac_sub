# Approved project infrastructure work-order handoff

Status: implemented native handoff with protected HTTP/admin adapters described
in [the staff adapter contract](PROJECT_ENGINEERING_STAFF_ADAPTERS.md). Deployment
acceptance remains a separate release gate.

`operations.project_work_order_handoff.issue` accepts immutable typed
`IssueProjectWorkOrder` and returns `ProjectWorkOrderIssued`. The command carries
canonical server `PermissionAdmission`, `CommandContext`, exact native Project
and optional task, expected `updated_at` preview tokens, current engineering
plan/revision/approval UUIDs and content digest, exact structural infrastructure
reference, typed approved location, explicit work classification, title,
description and governed priority. Context supplies a nonempty business reason
and owner-global idempotency key (at most 120 characters).

The scope is the existing exact `ProjectInfrastructure` target accepted by
`operations.project_engineering_plans`; no arbitrary greenfield route or multisite
scope is introduced. Fiber/radio is explicit. Survey locations have finite bounded
WGS84 coordinates; POP locations use native UUIDs. Issuance requires equality
with the approved typed location and infrastructure scope. PON parent OLT identity
remains approval-bearing through the immutable approved origin binding/digest.
See `PROJECT_ENGINEERING_PLANS.md` for criteria, current approval, reviewer policy,
supersession, revocation and target-lock rules. Those decisions remain wholly
owned by the engineering owner. Its `IssuanceEngineeringPlanQuery` participant
is restricted to the active handoff owner and hardwires staff dispatch authority;
it shares the existing freshness implementation. The ordinary public current
query retains engineering READ authorization. Dispatch admission grants no plan
review, policy mutation, draft access or broader historical engineering read.

One `execute_owner_command` owns issuance. Strict fresh staff/Person admission
under `auth.staff_permission_admission` checks canonical current RBAC, native
vendor exclusion, activity and write leave restriction. Context actor must equal
the admitted staff UUID and scope must equal `operations:dispatch:write`.
Engineering approval is distinct evidence from dispatch authorization.

The receipt-key advisory transaction lock serializes owner-global PostgreSQL
idempotency keys before origin locks. A unique key constraint is the final store
arbitrator. Native lock order is Project, exact optional task, engineering plan
head, then native target/location in stable table/UUID order, including PON parent
OLT. The coordinator uses Project's typed origin participant and engineering's
locked current-approval participant. Project on hold and blocked/done tasks fail
closed, with no supervisor override. Inactive, completed/canceled, mismatched or
superseded scope, stale preview tokens, revoked/superseded approval, changed
engineering bindings, scope/location/classification technology mismatches fail
before creation. Fresh staff/Person revalidation after domain lock waits is the
authorization linearization point. Authorization changes after that point govern
subsequent commands; retry after database concurrency failure is whole-command.

`operations.work_order_commands.stage_native_create` accepts separate explicitly
tagged fiber-verification and project-infrastructure command/provenance variants.
The existing customer/fiber contract retains its mandatory Subscriber and fiber
provenance. Outage creation retains its mandatory active origin Ticket. The
project variant requires the active handoff owner and delegates classification
to the canonical template owner. It creates a draft native infrastructure
WorkOrder with native Project/task FKs, explicit classification and no Subscriber
or Ticket. No second public classification command or nested executor runs.

`project_work_order_issuances` is dedicated immutable issuance provenance and
historical receipt, with exact project/task, approved plan/revision/decision and
digest, structural target, selected classification, canonical issuing staff/Person,
reason, command and issuance time. Location, acceptance criteria and PON parent
remain available through the exact immutable approved revision; no mutable
metadata bag becomes an authority. Composite FKs preserve origin/approval pairing.
The insert trigger verifies current approval and exact WorkOrder/approved-scope
pairing. ORM and PostgreSQL guards reject evidence updates/deletes. Subsequent
approval revocation/supersession never silently rewrites issued provenance.

Fingerprints cover every decision-bearing input, including actor/scope/reason,
origin preview tokens and exact approval/scope/location/classification. Transport
command/correlation IDs and carried permission lists are not intent. An exact
successful retry returns the original historical typed receipt only after current
staff dispatch authorization, even if current origin/approval now denies new
issuance. It creates nothing and emits no event. Different payload under the
same key raises a final idempotency conflict. A new key must pass current checks.

Receipt/provenance, canonical WorkOrder, audit and version-1
`project_work_order.issued` outbox event commit atomically. The event carries
opaque native lineage and digest, command/correlation and reason; no engineering
criteria content or customer identity. No implicit assignment, customer
notification, Ticket closure, restoration, provisioning, financial action or
Project/task completion is performed. The coordinator's issuance event is the
explicit consequence boundary; it does not emit the legacy customer creation
notification flow.

Migration676 is additive and backfills no issuance or approval. It uses five-second
lock and 120-second statement budgets; retry the whole migration on contention.
Downgrade refuses history destruction; retain schema and forward-fix.

Focused unit tests are a non-authoritative SQLite behavior lane. Architecture
guards protect one executor, canonical participant writes, dispatch/read admission
separation, unchanged customer/outage contracts and immutable additive schema.
`tests/integration/test_project_work_order_issuance_acceptance.py` is authored
PostgreSQL source for duplicate-key, revoke/supersession races, Project-before-task
lock observation, immutable evidence and rollback acceptance. The integrated
2026-10-11 dedicated-test-server run passed these cases as part of 46 PostgreSQL
acceptance tests, following both fresh migration and actual revision-674
predecessor upgrade through revision 678. Full repository and deployment gates
remain separate; source tests and SQLite do not establish those claims.

The protected staff API and admin issuance form obtain canonical admission and
server-owned origin/approval previews, submit the typed command on a
transaction-free session, and map owner errors while preserving refused input.
Their contract is documented in
[PROJECT_ENGINEERING_STAFF_ADAPTERS.md](PROJECT_ENGINEERING_STAFF_ADAPTERS.md).
These source capabilities do not authorize deployment or establish production
acceptance.


The protected `completion_issuance_evidence` query returns only the exact immutable
issuance receipt for the supplied issuance/work-order/public identities. Purpose
selects fresh completion READ or WRITE admission, independently of permission to
issue work. Unknown purposes refuse. Completion uses this proof for exact same
approved-scope replacement, including subscriber-free fiber/radio rollout; the
query neither grants new issuance eligibility nor reinterprets engineering approval.
