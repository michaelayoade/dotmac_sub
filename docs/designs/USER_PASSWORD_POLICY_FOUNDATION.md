# User password policy foundation

`auth.user_passwords` owns typed user-password representation verification and
explicit Argon2id parameter validation. `auth_flow.verify_user_password_outcome`
is the typed integration boundary; its boolean compatibility adapter retains
`COMPAT` by default. Existing password creation/reset owners still use the
unchanged PBKDF2 `hash_password` helper.

`REFUSE` must be supplied explicitly. It denies `plain:`, `enc:`, and supported
secret-reference prefixes before decrypting, resolving, or comparing material.
No setting, login notice, recovery campaign, or runtime policy flip is wired.
Existing reversible compatibility uses the existing helper. Service-secret
verification retains its existing bytes, bcrypt refusal, and protocol behavior.
Unknown, malformed, unsupported Argon2 variants/versions, and incorrect passwords
deny. Bcrypt retains its 72 UTF-8 byte limit with no truncation or prehashing.

Accepted bcrypt, PBKDF2-SHA256, and SHA512-crypt results classify as legacy upgrade
candidates. That classification grants no write authority. Reversible values
never classify as upgrade candidates, even when COMPAT accepts them. There is no
upgrade persistence in this foundation. The existing dormant representation
participant below remains disconnected from verification and login.

`hash_user_password` requires a typed `Argon2idHashRequest` with every tuning
parameter explicitly supplied and a fresh generated salt. It emits Argon2id v19.
Argon2 verification also requires explicit typed `Argon2idVerificationLimits`.
Absent limits deny Argon2 by default; maximum encoded length, memory, time,
parallelism, digest length and salt length are checked before native verification.
Future write activation and rollback must retain reviewed verification limits.
Test parameters are synthetic and are not production recommendations. Production
parameter selection requires the full benchmark/limiter acceptance work. New
password creation and opportunistic upgrades remain disconnected.

Strict fresh settings adoption remains blocked until Starter PR #797 produces a
verified released artifact; this code neither imports that unreleased API nor
changes the kernel pin. Activation also needs reviewed recovery/contact authority,
policy/audit wiring, and the explicit cutover decision. No deployment or production
credential changes are part of this slice.

`argon2-cffi` 25.1.0 is an explicit runtime dependency, matching the existing
locked main/dev package. The installed Poetry Locker refreshed the content hash
offline; the complete locked package graph remains unchanged. `poetry check
--lock` validates freshness without resolution or private-registry access.

The existing `auth.password_authentication.replace_password_representation`
participant accepts immutable `PasswordRepresentationReplacement` evidence:
a credential UUID, positive expected version, exact expected hash and supplied
replacement hash. Both representations are excluded from its repr and validation
errors. `PasswordRepresentationOutcome` returns only `REPLACED` or `CONFLICT`.
It never hashes, chooses parameters, verifies a secret, decides same-secret
eligibility, or retries a lost CAS. The eventual authorized caller must establish
same-secret eligibility before constructing this evidence.

The single exact hash/version CAS explicitly replaces `password_hash`; the
model's existing automatic `updated_at` timestamp also advances. It preserves
`credential_version` and `password_updated_at`, so an existing version-bound MFA
challenge remains valid. The existing transaction-local representation marker
is confined to a `Session.no_autoflush` window including setup, update, result
consumption and cleanup. Unrelated pending ORM changes remain pending; when the
caller later flushes a real secret change, the normal version trigger applies.
The caller owns the transaction: this participant never flushes, commits,
rolls back, creates a savepoint or enters a public owner command. Recoverable
client errors attempt marker cleanup before the original exception propagates.
When PostgreSQL has aborted the transaction, cleanup can also fail; that secondary
error never masks the original error, and the owner must roll back.

Hosted migrated-PostgreSQL tests in
`tests/integration/test_password_auth_credential_race.py` cover pending unrelated
secret changes on CAS success and loss, MFA/version/timestamp preservation,
client failures before and after SQL, later secret changes, and aborted database
transactions. Local synthetic tests exercise the same typed contract and error
identity; they provide no deployed-schema acceptance. No login caller, Argon2
parameters, default hashing policy or runtime activation is added.
