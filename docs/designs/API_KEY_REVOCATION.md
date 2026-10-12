# Administrative JSON API-key revocation

`auth.api_key_administration.revoke` owns only `DELETE /api-keys/{key_id}`.
The admin-only router mount and route-local `require_role("admin")` supply the
trusted user or API-key AuditActor. The adapter creates and closes a fresh
command session, so authentication reads and pending request writes do not
enter or get rolled back by the owner. Immutable `RevokeApiKeyCommand` carries
UUID target, actor, fixed authenticated-administrator provenance and matching
CommandContext with `api_key:revoke` scope. This context is internal provenance,
not a new assignable API-key scope.

The owner enters `execute_owner_command` once. It locks and refreshes ApiKey
with FOR UPDATE/populate_existing, sets is_active=false and revoked_at, and
stages canonical `auth.api_key.revoked` AuditEvent in the same root transaction.
Audit failure rolls back state and cannot return a success outcome. Audit carries
only target/actor identifiers, old/new active/revoked state and command/correlation
identifiers; never key material, hash, label or arbitrary request payload.
There are no external effects or outbox requirements for this slice.

Missing targets retain 404, success retains 204. Each successful repeated revoke
stamps a fresh timestamp and audit record, preserving prior behavior rather than
claiming replay deduplication. PostgreSQL lock waits are bounded by transaction-local
five-second lock_timeout; the adapter maps 55P03 to 503/Retry-After: 5. No automatic
retry; uncertain commit outcomes require inspecting state before resubmission.
The old ApiKeys.delete/revoke methods and all their consumers are retired.

Create/generate/PATCH, web revoke/rotate, system deletion participants and
last_used/hash-upgrade telemetry remain outside this bounded migration. No scope,
capability, minting, update or ambiguous web scope policy changes are included.

`tests/test_api_key_administration.py` proves the bounded same-root state/audit
trace with explicit protected_tables={api_keys}, rollback, trusted actor context,
missing target and repeated invocation. SQLite metadata is fast unit evidence
only. Migrated PostgreSQL concurrency and lock-timeout acceptance must execute
on the dedicated hosted test lane; local collection is not acceptance.
`tests/integration/test_api_key_revocation_concurrency.py` uses a real Alembic
heads clone and physical connection pinning to prove exact key serialization
through pg_blocking_pids, and rollback after the real five-second lock timeout.
