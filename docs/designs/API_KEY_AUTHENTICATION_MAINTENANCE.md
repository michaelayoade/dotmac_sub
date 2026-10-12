# Local API-key authentication maintenance

`auth.api_key_administration` owns the `local API-key authentication maintenance`
concern. The general and audit authentication adapters consume immutable typed
projections from a fresh owner-command Session. `AuthenticateApiKeyCommand`
contains only the presented secret (excluded from repr), GENERAL/AUDIT audience,
and fixed presented-key provenance. It accepts no claimed key ID, actor, scope
or caller context. The owner creates fixed internal CommandContext evidence;
any upgrade audit actor is the verified API-key UUID. Existing machine-first
credentials remain outside this local owner and retain their current behavior.

The scalar lookup selects only ID/hash/standing/expiry/subscriber/scopes/last-use,
never an ORM ApiKey or labels. Existing hash candidates, HMAC preference and SHA
compatibility without a server secret remain authoritative. GENERAL returns
exact existing scopes and subscriber ownership (the adapter retains key-ID
fallback, empty roles and wildcard-aware downstream permission behavior).
AUDIT requires exactly `audit:read` or `audit:*`; an unscoped, unrelated-scope or
bare `*` local key has no role bypass. JWT/session branches are unchanged.

Every public authentication call enters `execute_owner_command` exactly once.
If no HMAC upgrade is available/needed and last-use is younger than five minutes,
the owner returns the read snapshot without a row lock, UPDATE or audit. This
snapshot grants at its verification time; later revocation does not cancel an
already admitted in-flight request.

Cold authentication performs one conditional UPDATE matching the same observed
key ID and hash, active/unrevoked/currently unexpired standing, and an upgrade or
still-due timestamp. Only a SHA-to-HMAC upgrade sets key_hash. Timestamp CASE is
evaluated against the CURRENT stored timestamp and changes it only when still
due, preserving a concurrent newer touch. No label, scope or owner write occurs.
PostgreSQL waits are bounded by transaction-local five-second lock_timeout.
Expiry uses PostgreSQL clock_timestamp, plus final wall-clock projection checks,
so an expiry reached during a wait cannot grant. A returned AUDIT scope reduction
or expired standing raises an internal denial through executor rollback before
returning a denied typed outcome; maintenance is never committed for that refusal.

A CAS miss permits exactly ONE fresh scalar verification read by the same key
ID, presented candidate hashes and current standing. It accepts equivalent
upgrade/touch with the fresh current ownership/scopes or denies rotation,
revocation, expiry and deletion. It never falls back to the old ORM/snapshot,
retries UPDATE or loops. Timestamp-only maintenance is observability and has no
per-authentication canonical audit. An actual hash change stages one canonical
`auth.api_key.hash_upgraded` in the same transaction, with verified key actor and
target UUID, schema version 1, old/new scheme names, fixed internal scope/reason,
and command/correlation IDs. No hashes, raw keys, labels, email, scope values or
arbitrary caller payload enter audit. There are no external effects or outbox.

Audit/storage/commit failure returns no stale grant and rolls back maintenance.
SQLAlchemy errors become sanitized domain errors. The adapter preserves generic
401 for invalid credentials and maps maintenance outage to generic 503; lock
contention adds Retry-After 5. No automatic retry or unknown-commit replay claim
is made. A clean read transaction opened by this authentication invocation (including a
failed machine probe) is released before checking out the independent command
connection, preventing normal bounded-pool self-starvation. Adapters capture
transaction presence before any probe/read: preexisting caller transactions,
including already-flushed writes, are neither committed nor discarded. Such
callers retain their own connection while the independent command executes. Both legacy
last-use/hash-upgrade ORM mutation helpers are removed after full consumer search.

Fast inert-key unit tests prove atomic upgrade/audit, hot zero UPDATE/audit,
five-minute boundary, no-secret compatibility, timestamp monotonicity, one-read
CAS recovery, fresh scope/ownership, exact AUDIT gates, rollback/privacy,
independent sessions and existing machine-first regression behavior. Migrated
PostgreSQL tests pin physical connections and observe exact pg_blocking_pids
barriers for both-order rotate/revoke/scope/delete races, competing touches and
expiry while waiting. They are collected locally only; hosted real-Alembic
execution is mandatory acceptance. SQLite metadata is not deployed-schema proof.
This slice does not retire password compatibility, change Argon parameters,
select runtime secrets, execute native hashes/benchmarks or close customer
API-key deletion ownership or the wider security program.
