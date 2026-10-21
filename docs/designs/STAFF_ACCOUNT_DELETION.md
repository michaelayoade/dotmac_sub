# Inactive staff account deletion

`auth.staff_provisioning` owns only administrative single/bulk inactive
SystemUser hard deletion. Frozen typed commands carry UUID targets, trusted
AuditActor, fixed authenticated-administrator provenance and matching internal
RBAC CommandContext. Routes derive the actor from the existing trusted system
route helper, never the target. The inherited admin guard and route-local
`rbac:assign` guards remain. Adapters create/close fresh sessions; request reads
and pending writes do not enter or get rolled back by deletion. Each public
command enters `execute_owner_command` once; all collaborators only flush.

Single missing targets retain 404; active accounts retain the existing blocked
response (including HTMX toast). Bulk missing and active accounts are skipped.
All eligible deletions share one transaction: any late FK, audit or storage
failure rolls back the entire batch, including earlier child/key deletes and
all audit rows. There are no per-row savepoints or partial success commits.
Successful single deletion retains HTMX redirect or ordinary 303; bulk retains
the existing count/message JSON shape. Malformed UUID input retains its prior
failure class. SQL errors are sanitized domain errors; linked records map to a
blocked response/409, five-second PostgreSQL lock timeout to 503 Retry-After: 5,
other storage failures to 500. No automatic retry or durable replay; inspect
state and audit after uncertain commit outcomes.

Bulk IDs are deduplicated for locking/deletion; additional occurrences count as
skipped. This fixes a characterized reporting bug: the exact retired helper at
base 87ac5fc returned deleted=2/skipped=0 for [id,id], while its second physical
DELETE matched zero rows and emitted an SQLAlchemy warning. The new result is
deleted=1/skipped=1. It does not claim two deleted principals or duplicate audits.

Lock order is all unique existing selected SystemUser rows sorted UUID with
FOR UPDATE NOWAIT/populate_existing before any dependent or shared admin-role lock.
Inactive standing is rechecked on these refreshed rows. Then credentials, MFA
methods and sessions are locked by scalar ID in deterministic UUID order before
deleting exact rows, followed by UUID-sorted ApiKeys through the named
`auth.api_key_administration.stage_staff_key_deletion` participant, typed
`auth.system_user_assignments.remove_all_for_system_user` cleanup and principal
deletion. The assignment participant preserves the existing last-active-admin
check with one deletion-only admin Role FOR UPDATE NOWAIT query. Other auth/RBAC
callers retain ordinary blocking behavior. No Subscriber, Party, email advisory
or new global lock is acquired.
JSON PATCH changes subscriber_id but never system_user_id, so its key lock/FK
check cannot invert the staff-principal-first hierarchy. Existing key revoke and
web rotation share the exact key lock. Concurrent auth-child insertion resolves
through the existing principal FK check. Principal and admin Role contention fail immediately with deletion_busy;
other child/key lock waits retain transaction-local five-second lock_timeout.
NOWAIT prevents deletion from waiting while holding one selected principal: an
in-flight RBAC writer can hold target B/admin Role then request a grantor A FK
KEY SHARE, which otherwise cycles against single A deletion waiting for Role
or bulk A+B deletion waiting for B. A busy refusal rolls back all prior changes
and releases A so that writer can finish; a retry requires current-state review.

The cleanup set matches the two retired staff helpers: UserCredential,
MFAMethod, AuthSession, ApiKey, RBAC grants and SystemUser. MFARecoveryCode's
existing NO ACTION FK remains a blocker; no recovery-code cascade is invented.
Technician, ERP and other business-history FKs likewise remain blockers. Native
Party identity and global AuthenticationBinding records are preserved; no
Party-wide deletion or shared principal ownership policy is introduced.
Generic recovery/invite intents are not deleted: existing transport-time
materializers must continue to reject vanished exact staff principals.
Customer deletion paths are untouched and require their own design.

Canonical `auth.staff_account.deleted` and one `auth.api_key.deleted` per actual
key removal commit with authoritative deletion. Evidence contains only target
and actor identifiers plus command/correlation IDs, schema version 1, fixed
`scope=rbac:assign` and `reason=inactive_staff_account_deletion`. Caller-supplied
context reasons are excluded, as are email, display name,
credential hashes, seeds, key material, labels or scopes. There is no external
consequence or deletion outbox requirement. Existing auth cache invalidation
runs only after commit. The route's post-commit PII audit and both legacy staff
delete helpers are removed after full repository consumer search; the assignment
helper's only consumers migrate to the typed parent-owned participant.

Fast unit tests trace same-root protected deletions, audit/commit/late-bulk
rollback, stale standing, exact safe evidence, duplicate counts, dual-bound keys,
retained Party/subscriber identity, existing recovery-code blocker, fresh adapters
and real HTTP admin/permission/actor denials. SQLite metadata is not schema
acceptance. `tests/integration/test_staff_account_deletion_pg.py` requires hosted
real-Alembic PostgreSQL execution: both-order PATCH/revoke/web-rotate key races,
overlapping reverse bulk busy/retry, activation busy/standing refresh, key lock
timeout, single/bulk real RBAC grantor-FK cycle refusal, recovery-code
and technician FK whole-batch rollback, parent-audit rollback and retained
Party/subscriber identity. Local collection alone is not acceptance.
