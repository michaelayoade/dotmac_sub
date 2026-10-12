# Administrative JSON API-key issuance, update and revocation

`auth.api_key_administration` owns JSON `POST /api-keys`,
`POST /api-keys/generate`, `PATCH /api-keys/{key_id}` and
`DELETE /api-keys/{key_id}`.
The revocation command owns `DELETE /api-keys/{key_id}`.
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

Customer/system deletion participants and
last_used/hash-upgrade telemetry remain outside this bounded migration. No scope,
capability, update or ambiguous web scope policy changes are included.

`tests/test_api_key_administration.py` proves the bounded same-root state/audit
trace with explicit protected_tables={api_keys}, rollback, trusted actor context,
missing target and repeated invocation. SQLite metadata is fast unit evidence
only. Migrated PostgreSQL concurrency and lock-timeout acceptance must execute
on the dedicated hosted test lane; local collection is not acceptance.
`tests/integration/test_api_key_revocation_concurrency.py` uses a real Alembic
heads clone and physical connection pinning to prove exact key serialization
through pg_blocking_pids, and rollback after the real five-second lock timeout.


## JSON issuance

`create` accepts frozen/slotted `CreateApiKeyCommand` with explicit fields;
`generate` accepts `GenerateApiKeyCommand`. Both carry a trusted administrative
AuditActor, fixed authenticated-administrator provenance and matching internal
`api_key:create` / `api_key:generate` CommandContext. These are provenance scopes,
not new assignable permissions. Both route-local and router admin guards remain.
Adapters open independent command sessions; each public command invokes the
owner executor exactly once. The shared issuance participant only flushes.

Create continues to hash the supplied `key_hash` **preimage** with the established
`hash_api_key` helper. Generate continues to mint `token_urlsafe(32)` and hash it
with that helper. No hashing scheme or server-secret policy changes. Existing
subscriber existence checks, alias handling, label, scope list order/duplicates,
expiry, inactive/revoked/last-used create fields and generated active defaults are
preserved. Empty scopes remain fail-closed at runtime; arbitrary existing scope
strings, wildcard handling and past expiry acceptance are unchanged. No staff
binding, new policy, default scope, or web None-wildcard correction is introduced.
New-row INSERT, unique key-hash constraint and subscriber FK are the issuance
concurrency backstops; no lock on an existing API key is needed. Concurrent
subscriber deletion still resolves through the existing FK, without a new policy.

Generation calls the existing Redis admission logic on the request read session
before opening the fresh command session or generating secret material. Redis
unavailable/error remains 503, excess remains 429, and the original client-IP,
window clamping, count/expiry ordering and settings reads remain. A downstream
transaction failure consumes admission capacity, matching the prior behavior;
Redis is not a participant or authority for API-key state.

API-key INSERT and canonical `auth.api_key.created` / `auth.api_key.generated`
AuditEvent commit together. The audit carries target/actor identifiers, active
state and command/correlation identifiers only: no raw material, hash, label,
subscriber payload or scopes. There are no external side effects or EventStore
requirements. Audit staging or commit failure rolls back both rows and returns
no outcome. Outcomes are immutable detached typed snapshots, so serialization
after session close needs no database read. Secret/hash/label fields are excluded
from command/outcome repr. Generated material appears only in the existing
one-time response after commit; ApiKeyRead preserves its existing hash masking.
Missing subscriber remains 404 and success 201. SQLAlchemy storage/constraint
failures retain HTTP 500 through a sanitized domain error without bound SQL
parameters or a printed exception chain. There is no idempotency claim,
automatic retry or generated-secret replay; unknown commit outcomes require
inspection rather than blind resubmission.

Repository-wide consumer search found the two JSON routes and tests as the only
callers of `ApiKeys.create/generate/generate_with_rate_limit`. Those legacy writers
are removed; existing tests now use typed owner fixtures. The admission-only
`enforce_generation_rate_limit` helper retains Redis behavior. Customer/system-user deletion participants and authentication last_used/hash
upgrades remain separate ownership debt, not alternate issuance fallbacks.

`tests/test_api_key_issuance.py` covers protected-table transaction traces,
before/after audit-staging and commit rollback, malformed/untrusted commands,
subscriber errors, unique-hash refusal, preserved fields, fresh route sessions,
secret-free evidence/repr and rate-denial ordering. SQLite is fast unit evidence
only. `tests/integration/test_api_key_issuance_pg.py` is collected locally only;
real migrated PostgreSQL acceptance belongs to hosted CI.


## JSON PATCH

`update` owns only the existing admin-gated JSON PATCH. Frozen/slotted
`UpdateApiKeyCommand` carries a UUID target, closed `ApiKeyUpdateField` selection
set, precise nullable values, trusted actor, fixed administrative provenance and
matching internal `api_key:update` context. The adapter preserves `key_id: str`
parsing status (malformed path remains HTTP 500) and `model_fields_set` records
exactly which schema fields were supplied. There is no invented `person_id` alias
or `system_user_id` input. Commands and outcomes exclude label, secret/hash,
actor label and arbitrary scope values from repr.

The fresh adapter session enters the owner executor once. PostgreSQL gets a
transaction-local five-second lock timeout, then a target `FOR UPDATE` read with
`populate_existing`. Explicit assignments permit only subscriber_id, label,
key_hash, scopes, is_active, last_used_at, expires_at and revoked_at. Omitted fields
remain unchanged; explicit null is assigned. A nonempty key_hash is a preimage
hashed by the existing helper. Existing subscriber existence checks remain;
subscriber ownership may transfer or clear, and system_user_id is retained even
when this produces a doubly bound key. No XOR rule, new authority, permission
catalogue/wildcard validation, reactivation refusal or expiry policy is added.
Setting is_active=true and clearing revoked_at still reactivates a key; past
expiry, direct last_used timestamps, unknown/wildcard and duplicate scope values
remain accepted. Empty/no-op PATCH still produces canonical audit evidence.

`scopes` uses JSON(none_as_null=False). Explicit scopes=None therefore commits
JSON **null**, not SQL NULL, despite the non-null database column. The detached
typed outcome represents this state. The unchanged ApiKeyRead schema cannot
serialize null scopes, so this retains the existing post-commit HTTP 500 class
with safe response-error text; canonical state/audit remain committed. This is
characterized compatibility debt, not a new scopes policy. Explicit key_hash=None
or is_active=None reaches the existing SQL NOT NULL constraint and rolls back
with HTTP 500. Other nullable fields clear normally. Missing target/subscriber
remains 404; success remains 200 with existing hash masking. SQLAlchemy storage
failures become sanitized typed errors without a printed bound-parameter chain;
55P03 lock timeout maps to 503 / Retry-After: 5 after rollback. No automatic retry;
unknown commit results require inspection before resubmission.

Canonical `auth.api_key.updated` AuditEvent commits with the key change. Details
allow only sorted requested field names from the closed enum and an actual
hash_changed boolean, plus command/correlation identifiers. Target/actor IDs are
canonical audit fields. Owner UUIDs, scope values, raw/stored hash, labels, old/new
request values and arbitrary payloads are excluded. No additional EventStore or
external consequence is introduced. Audit middleware's existing best-effort
request PATCH event remains separate supplementary evidence.

Locking is target-first and existing subscriber existence lookup remains a plain
read; a changed subscriber_id invokes PostgreSQL's normal FK key-share check.
Unique hash/FK constraints remain authoritative backstops. No locks or writes to
system_user_id are introduced. Customer deletion participants in
web_customer_actions and system-user deletion participants in
web_system_user_mutations still delete ApiKey rows independently, while auth
telemetry remains a separate writer. This bounded slice
proves target PATCH/revoke serialization, not all deletion/owner-transfer races.

Repository-wide search found only the JSON route plus two tests calling
ApiKeys.update. The legacy writer is removed and tests use typed update fixtures.
`tests/test_api_key_update.py` covers exact omission/null semantics, reactivation,
owner collision retention, safe canonical evidence, stale identity-map refresh,
auditing/commit rollback, typed denials, fresh route session and status classes.
SQLite evidence is non-authoritative. Five migrated PostgreSQL cases in
`tests/integration/test_api_key_update_pg.py` are locally collected only: exact
backend PATCH/revoke serialization, lock timeout rollback, JSON null storage and
two SQL NOT NULL rollback cases. Hosted execution remains required acceptance.


## Web creation, revocation and rotation

The existing permissioned `/admin/system/api-keys` POST routes now use
`web_create` and `web_mutate` on this same owner. The inherited admin guard and
route-local `system:settings:write` guards remain. Frozen typed commands carry
an AuditActor derived only from the existing `_system_command_context` trusted
actor helper, matching prefixed actor context, fixed administrative provenance,
UUID target/owner and closed action/caller scope values. No actor is inferred
from a target key or caller subscriber identity. Each adapter opens and closes
an independent command session; request reads and pending writes are untouched.
The owner enters the executor once, and helper participants only flush.

Creation preserves comma/space scope parsing, label, token_urlsafe(32), existing
hash helper and expiry semantics, including zero/negative days. Invalid integer
input still renders the form; unchecked own creation without a person_id still
redirects, while checked system creation uses a null subscriber. Secret material
is excluded from repr and appears once only after state/audit commit in the POST
page. Rotation replaces the hash in place and preserves owner, staff binding,
label, scopes and expiry, including an already-expired active key. Revoked or
inactive keys refuse rotation. Repeated revocation stamps and audits again.
Missing/out-of-scope revoke keeps its banner redirect; unsuccessful rotation
keeps its plain redirect. List ordering and visibility remain unchanged.

All null-subscriber keys remain shared, including staff-bound keys. A truthy
caller subscriber identity permits only its own subscriber keys plus those
shared keys. Empty staff person_id preserves the characterized failure on a
subscriber-owned target, but succeeds on shared targets. The retired helper's
None wildcard/default was not an ordinary authenticated route identity; it is
not exposed by the new caller scope. This adds no private staff-owner policy.

Creation retains the settings resolver's existing cached reads, nonnegative cap
coercion (bad data disables caps), count of active/non-revoked keys and max-TTL
rejection/defaulting. The null-subscriber count includes staff-bound keys.
These are **best-effort web admission checks**: no count lock or new global
quota guarantee. Concurrent web creation can exceed the count cap, JSON issuance
bypasses it, and JSON PATCH can transfer/reactivate keys. Strong global caps
would require a separately reviewed cross-writer contract. Subscriber existence
is a plain lookup with FK race backstop; no new principal/advisory locks.

Revocation/rotation use exact ApiKey FOR UPDATE with populate_existing before
checking scope/standing. They do not change a principal FK or acquire a parent
lock. This preserves the existing child-first deletion/PATCH lock order rather
than introducing a principal-before-child inversion against JSON PATCH's FK
key-share checks. Transaction-local PostgreSQL lock_timeout is five seconds.
Storage exceptions are sanitized domain errors; mutation busy maps to 503 with
Retry-After: 5. Creation retains form-error rendering with safe error messages.
There is no automatic retry, durable replay or secret re-display after uncertain
commit outcomes.

Canonical `auth.api_key.created`, `auth.api_key.revoked` or
`auth.api_key.rotated` evidence and state commit atomically. Web evidence contains
only active state, target/actor IDs and command/correlation IDs, never secrets,
hashes, labels, scope values or owner payloads. No external event or outbox is
required. Legacy create/revoke/rotate helpers were removed after repository-wide
consumer search; form context/scope parsing and list reads remain. Customer/staff
deletion and auth last_used/hash-upgrade telemetry remain separate migration debt.

`tests/test_web_api_key_administration.py` covers same-root state/audit traces,
audit/commit rollback, secret-free evidence, scope/standing refresh and composed
actual Jinja POST renders/redirects. Existing form cap and scope tests use typed
owner fixtures. SQLite is fast unit evidence only. Four new cases in
`tests/integration/test_web_api_key_administration_pg.py` require hosted migrated
PostgreSQL acceptance: exact-key web rotation/JSON revoke serialization in both
orders, lock-timeout rollback and creation/audit rollback. Local collection is
not deployed-schema acceptance.
