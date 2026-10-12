# Authenticated subscriber MFA enrollment

`auth.password_authentication` owns `authenticated subscriber MFA enrollment`.
Setup and confirmation have separate immutable commands, UUID identifiers,
closed adapter provenance, and mandatory authenticated AuthSession proof. Their
internal `authentication:mfa:self` scope is not an assignable RBAC permission.
The actor must be the subject subscriber. API keys, staff, reseller-user, and
impersonation contexts cannot enter this subscriber command; native reseller
and staff enrollment remain with their existing flows.

Adapters normalize established authentication context and open a fresh owner
session. Each command enters `execute_owner_command` once. Locks are refreshed
Subscriber FOR UPDATE -> exact AuthSession FOR UPDATE -> factors in UUID order.
The session must still be active, unrevoked, unexpired and belong exclusively
to the subscriber. This second check closes the middleware/reset window: a
reset that wins the principal lock revokes the in-flight session before setup
or confirmation may restore a factor. The reciprocal ordering lets a winning
enrollment finish before reset removes it. Refresh only locks sessions and
never acquires a later principal lock. PostgreSQL lock_timeout is five seconds;
contention returns a retryable refusal and no automatic retry.

Setup reuses the newest active, unverified, disabled TOTP row, or inserts one,
and replaces its server-generated encrypted seed while locked. Confirmation
checks exact ownership, type, active state, seed and lockout after locking and
before decryption/TOTP. Active verified reconfirmation remains supported. It
clears other primaries before enabling the target, records verification time,
and clears failure counters. Web `RETURN_ONCE` recovery codes are staged with
the existing flush-only participant in the same commit; API `NONE` issues none.
Secret, URI, submitted code and returned recovery codes are excluded from repr.

Both transitions stage canonical AuditEvent and the existing safe
`credential_administration.completed` event intent with
`invalidate_sessions=False`; the registered credential-session projection
invalidates cache state without revoking sessions. Events contain only IDs,
a bounded transition/provenance, command evidence and no seed/code. On wrong
code the enrollment transaction rolls back and releases locks first, then the
independent typed denial owner atomically stages failure counter and audit.
Missing, foreign or revoked factors never create a failure-counter effect.

No schema migration or backfill is required. Setup/confirmation never repair
revoked factors; reset's existing atomic audit is preserved. Legacy subscriber
`AuthFlow.mfa_setup` and `mfa_confirm` writers are retired. Non-authoritative
SQLite/unit and composed rendering tests provide fast feedback. Only hosted
PostgreSQL migrated through the checked-in Alembic chain can establish locking,
reset/enrollment winner ordering, timeout and deployed-schema acceptance.


The same existing authentication owner resolves `principal-specific MFA profile
projection` from a closed Subscriber/ResellerUser principal query. Its immutable
summary contains only the existing JSON MFA fields. The reseller API serializes
this projection after either native or subscriber confirmation, so a native
reseller-user factor is selected by reseller_user_id and reported enabled.
Native writers remain unchanged. The generic legacy organization-profile path
continues outside this slice; its MFA fields are replaced by this owner summary
at the touched enrollment response boundary.
