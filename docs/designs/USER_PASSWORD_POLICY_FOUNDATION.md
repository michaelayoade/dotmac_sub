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
upgrade hook or persistence in this foundation.

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
