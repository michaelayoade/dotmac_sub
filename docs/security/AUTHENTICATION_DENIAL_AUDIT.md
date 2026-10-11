# Authentication denial audit contract

`auth.password_authentication.record_authentication_denial` owns failure-side
commands under the existing authentication failure concern.
`observability.audit_log` remains the AuditEvent persistence owner. EventStore
is not required audit evidence. This source slice does not close Q-A4 (general EventStore/receipt evidence
semantics) or Q-A6 (CLI attribution). Audit immutability and retention remain
separate work.

The adapter first decides a typed denial and snapshots trusted target UUIDs.
A failing success owner rolls back before the adapter dispatches the failure
command once. The failure command opens `owner_command_session()` from the
application engine, independently of the request's active connection. It never
releases, commits or rolls back request work, including already-flushed writes.
Its optional Core counter UPDATE and failed AuditEvent commit in one owner
transaction. Audit unavailability rolls back the new increment; existing locks
remain. There is no unaudited counter fallback. Other refusals stage audit only.
Session lifecycle, audit, flush, commit and telemetry failures preserve refusal.
No new step-up lockout policy is introduced. Untyped internal owner/audit
exceptions are operational failures outside this bounded denial slice; they
are re-raised unchanged and never classified as authentication refusals or
written as denial AuditEvents.

The evidence actor is the system component `auth.password_authentication`.
A resolved credential or method is the target, not an authenticated user actor;
no Party enrichment is inferred. Metadata is an allowlist: schema version,
enum reason/surface/phase, target UUID, enum provenance, attempt UUID and
correlation UUID. Access fallback verification carries a typed resolved-source outcome across
refusal; unknown/ambiguous sources have no target. Query identifiers and source
secret representations are excluded from value-object reprs.
A request correlation header must parse as a UUID; malformed
input is replaced, never truncated or retained. Client IP is normalized from
the trusted request client, not forwarded headers. Status is bounded and
`is_success` is false. Passwords, usernames, hashes, versions, cookies, JWTs,
URLs, raw headers, SQL parameters and exception text are excluded. Failure logs
contain a fixed event name without traceback. The write-failure and unknown
counters have no labels. Unknown/ambiguous identities and invalid unsigned
challenges create no identity audit row. Validated challenge targets must also
resolve to an existing canonical source record before becoming audit evidence.

## Bounded dispatch inventory

| Entry point | Explicit decision coverage |
| --- | --- |
| `AuthFlow.login` | Known wrong password/RADIUS failure with counter; uniquely resolved access-credential mismatch without a local counter; lock, reset-required, disabled, staff projection, audience and phase-B refusal without counter; unknown/ambiguous aggregate only |
| `AuthFlow.mfa_verify` | Invalid challenge aggregate; validated audience/projection/method/lock refusal; phase-B typed refusal after rollback, with one counter only for invalid code |
| `AuthFlow.admin_mfa_confirm`, `reseller_mfa_confirm`, `mfa_confirm` | Missing/mismatched/unsupported/locked method and wrong code; counter+audit on an independent owner session replaces legacy counter-only commit |
| `web_auth._mfa_enrollment_binding` / enrolled session | Invalid challenge, projection and audience; version-bound enrollment session owner refusal |
| Customer portal password / MFA | Local wrong-password counter; lock/reset/status/identity refusal; MFA challenge/context/method/lock and owner refusal; external credential sources retain separate counters |
| Vendor API / browser login | Known preadmission denial retains resolved credential UUID, including nonstaff; posttoken membership denial records a DB-resolved principal without asserting session rollback |
| Reseller posttoken admission | DB-session-resolved principal records membership/subscriber denial after issuance, without asserting session rollback |
| Current password change / profile / inspector step-up | Known wrong current-password audit only; no new counter policy |

The password owner also exposes typed immutable portal source snapshots before
network verification. This replaces the two R-PR2 direct adapter snapshot reads
without increasing the direct-query baseline or retaining ORM records across
that boundary.

The table is source coverage, not PostgreSQL acceptance. Fast unit tests use
file SQLite and independent engine sessions to exercise real counter+AuditEvent
atomicity and request-write preservation. Existing savepoint-fixture behavior
tests are logic evidence only. The deployed-schema/concurrency gate requires a
named migrated PostgreSQL target; none was assigned for this implementation.
`tests/integration/test_authentication_denial_audit.py` adds independently
collected tests for exact failure/request backend PID separation, pending and
flushed request-write isolation, audit-outage rollback and concurrent exact
counter/audit counts. They remain unexecuted without the migrated target.
