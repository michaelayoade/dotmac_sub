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

## Dormant native-password admission foundation

`auth.user_passwords.NativePasswordAdmission` owns the policy concern
`explicit native password admission`. Construction requires an immutable typed
`LimiterConfiguration` supplying all five values: native capacity `k`,
`maximum_waiters`, `admission_timeout` in seconds, `threadpool_tokens`, and
`reserved_tokens`. There are no production defaults. Capacity and threadpool
size must be positive integers; waiter and reserve counts are nonnegative
integers; booleans are invalid. Timeout must be finite and nonnegative.
The budget must satisfy
`k + maximum_waiters <= threadpool_tokens - reserved_tokens`.

An explicitly constructed instance bounds active scopes and waiting callers
separately. `admit()` yields immutable `PasswordAdmissionOutcome` evidence with
measured monotonic queue-wait seconds and no identity, password, or hash data.
A full waiter budget raises typed `PasswordAdmissionBusyError` with stable code
`auth.user_passwords.admission_saturated`; an expired wait raises
`auth.user_passwords.admission_timed_out`. Invalid configuration raises
`InvalidLimiterConfigurationError` with code
`auth.user_passwords.invalid_admission_configuration`. There are no automatic
retries or transport mappings. Scope exit releases capacity in `finally`,
including exception or synchronous cancellation unwinding. Waiting cancellation
also returns its waiter budget. There is no FIFO fairness claim.

This synchronous primitive has no singleton, global context, default instance,
installed login/hash caller, settings adoption, or database transaction. It does
not enforce the actual framework threadpool size or coordinate worker processes.
Future adoption must share one reviewed instance per process, account for every
worker and its threadpool, and acquire/release separately for verification and
any later rehash. A scope must cover only its one native operation, with no
nested acquisition or unrelated work/transaction inside it. Async adapters must
ensure cancellation cannot release a scope while its native thread still runs;
this foundation neither cancels nor installs native work.

Event/barrier unit tests use synthetic scopes, bounded thread counts and no
native hashes or database. They prove accounting/refusal/release behavior, not
worker/Celery performance, production memory headroom, tuning, deployment
acceptance, or safe activation. Existing password algorithms, flags, policy,
strict settings adoption and service-secret/PPPoE behavior remain unchanged.

## Dormant password-work observation boundary

`UserPasswordWork.verify(UserPasswordWorkRequest)` is a typed, immutable-input
contract owned here. Both password and stored representation are excluded from
request repr. `LegacyPasswordWork` permanently uses COMPAT without Argon limits
or upgrade work. `AdmittedArgon2PasswordWork` requires explicit typed verification
limits, admission instance, observer and existing compatibility verifier; it has
no defaults, singleton, settings, environment flags or installed runtime caller.

The same canonical parser/policy path validates encoded size, exact PHC grammar,
parameters, type, version and encoding before admission. Salt and digest must use
canonical unpadded Base64: strict decoding, actual decoded-length checks and exact
re-encoding reject malformed alphabets, padding and nonzero unused trailing bits.
Parameter fields use the canonical m,t,p order with positive decimal integers,
no duplicate fields, signs, leading zeros, whitespace or trailing data. Only the actual low-level `verify_secret`
call is timed, with monotonic start/end inside the admitted scope. Native failure
also records its interval. Malformed input, policy rejection and admission refusal
record no interval. Compatibility parsing, reversible resolution and legacy
callbacks are excluded. Capacity remains held until native completion, including
BaseException unwind; observer delivery happens after release. Observations carry
only operation kind, monotonic start/end and admission wait seconds, never secret,
identity, request, hash or exception text. No native hash work is added.

`NativePasswordObserver.observe` acknowledges RECORDED or INCOMPLETE. Rejection,
invalid acknowledgement or any observer exception permanently marks the work
instance incomplete while preserving the verification result or original error.
A collector MUST drain all callers then invoke `require_complete_capture()` before
accepting measurements; it raises `IncompletePasswordCaptureError` after any lost
record. Successful acknowledgement establishes delivery only, not durable storage
or completeness of an external collector. Collector integrity remains required.

This boundary is dormant/uninstalled and provides no performance acceptance.
Login/API/web/`complete_password_step` remain unchanged. Existing verification
can hold an implicit read transaction until completion: safe future installation
requires a separately reviewed snapshot/revalidation design before native work,
plus acceptance capture, fixture/executor and operational authorization. There is
no prepared replacement, CAS wiring, persistence, upgrade or parameter selection.
Architecture guards refuse production imports/use of the dormant work contracts;
fake-native tests alone establish source behavior, not real hashing performance.
