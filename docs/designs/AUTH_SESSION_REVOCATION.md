# Atomic database authentication session revocation

Owner: `app_sessions.auth`, implemented by `session_manager.revoke_authentication_sessions`.

The immutable command carries correlation context, typed principal kind and intent,
audit actor provenance, exact target/current UUIDs and an ephemeral repr-hidden
refresh bearer. Logout resolves user accountability from the exact current bearer;
vendor admission refusal is SYSTEM `vendor_admission`. Authenticated self commands
revalidate the live current session and exact principal binding. Administrative
DELETE preserves the authenticated user or API-key actor. No missing actor is
inferred as a user.

A fresh adapter-owned session enters `execute_owner_command` once. Principal
`FOR UPDATE` precedes stable UUID ordered session locks. All three principal
foreign keys must agree with the selected binding, including NULL on the other
two columns. The principal lock conflicts with issuance's explicit/implicit
foreign-key KEY SHARE lock, serializing signout-others against new issuance.
The current token hash alone authorizes logout; previous rotated tokens gain no
new authority. Repeated administrative revocation preserves transition time, including NULL when historical time is unknown. Empty all-others returns `revoked_at: null`, records `auth.sessions_revocation_noop` with typed `noop` disposition and emits no projection intent. Staff UI reports no other active sessions rather than a new transition.
Missing, malformed, expired or cross-principal current IDs cannot revoke others.

The canonical `audit_adapter.stage_audit_event` INSERT, authoritative status
transition and version-1 `authentication_sessions.revoked` outbox intent commit
atomically. Failed audit, flush or commit rolls them back without cache cleanup.
PostgreSQL lock timeout 55P03 maps to 503 with Retry-After 5 in each adapter.

`SessionRevocationProjectionHandler` receives only exact revoked session UUIDs,
principal kind/UUID and correlation evidence. It rechecks committed revoked
standing and uses strict exact-session cache invalidation. Redis failure propagates
for durable dispatcher retry. It never invalidates a principal epoch/index wholesale,
so retry cannot revoke later logins. Database session standing remains the deny
authority while cache delivery is unavailable. No bearer/hash/cache key is in
outbox or audit payloads. Audit actor Party enrichment is deliberately absent;
no Party lock is needed and no actor identity is fabricated.

## Transport coverage and remaining work

Covered: ordinary AuthFlow bearer logout; auth API logout; staff web logout;
vendor API logout; vendor refusal compensation; self API single/all-others;
staff profile all-others; retained administrative auth API DELETE.

Residual, excluded: customer/reseller portal logout and signout-others, reseller
portal database standing, impersonation, MFA and webhooks. Generic session
POST/PATCH retirement lands in its separate prerequisite PR and must not be
reintroduced when rebasing this change. Existing refresh, reset and credential
owners retain their own transitions; this slice does not claim their audit debt
is retired.

`tests/test_atomic_session_revocation.py` is the fast SQLite logic lane and
invocation-scoped actual SQL transaction evidence. It is not migration or
PostgreSQL concurrency acceptance. Hosted PostgreSQL tests must use
`cloned_database('heads')`; no local PostgreSQL, Docker, SSH or production test
execution is authorized by this slice.

Hosted source coverage: `tests/integration/test_atomic_session_revocation_concurrency.py` collects six exact blocking-graph cases (logout/refresh, revoke/revoke, others versus OIDC/reset/password issuance, and refresh-first immediate-session-lock ordering). Local collection is not hosted acceptance.

Consumer search: server self-revocation transports forward `SessionRevokeResponse`; the mobile auth repository ignores DELETE bodies, so nullable no-op timestamps require no parser change. Mobile session-screen success copy remains a client UI follow-up outside this server slice.
