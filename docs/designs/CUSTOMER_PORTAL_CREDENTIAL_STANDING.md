# Customer portal credential standing (R-PR2)

`auth.password_authentication` owns local password success and customer MFA
completion. Its typed verified snapshot enters one owner transaction: principal
key-share lock, credential-version gate, eligibility recheck, AuthSession insert,
and audit. MFA pending creates only a bound v2 challenge and password-step audit;
it neither inserts a session nor sets last_login_at. Customer failure counters use
the same atomic owner with the existing customer policy values.

The Redis projection is written after commit and carries auth_session_id.
`customer_portal_session.get_current_customer` reloads that primary-key row on
every request and requires the same subscriber/account, active status, no
revoked_at, and a future expiry. Refresh also checks standing. Database expiry is
fixed at issuance to the configured initial portal TTL (including remember-me);
Redis sliding refresh cannot extend it. Reset already revokes AuthSession inside
its transaction, so the next request fails before any asynchronous epoch handler.
Failed Redis delivery returns 503 without a cookie and best-effort revokes the
committed session through the same owner. A failed compensating revoke leaves an
undelivered, unexposed row subject to database expiry; it does not return success.

PPPoE and RADIUS network protocols and secret storage remain unchanged. Neither touches
local UserCredential counters. Customer MFA binds the source to a v2 challenge:
local cid/cv; access acid/acu; RADIUS rid/ru plus its linked acid/acu. RADIUS
snapshots precede network authentication. The strict microsecond updated_at
markers intentionally also invalidate challenges after unrelated source updates;
there is no secret or digest in the challenge. RADIUS standing locks the linked
AccessCredential then RadiusUser, after the principal key-share lock, and checks
active status, subscriber, linkage, and both markers. Access-only gates also
hold a row lock and check the strict marker; a source update after the gate
waits until the session transaction finishes. If RADIUS succeeds without a
pre-network RadiusUser observation, the submitted password must additionally
verify against the immutable pre-network AccessCredential representation
through verify_service_secret before that access source can justify a session.
This fallback retains the service verifier's bcrypt refusal, even when an
external RADIUS server accepted the password; local user bcrypt compatibility
does not justify accepting bcrypt service material. A newly appeared or mismatched
source is denied rather than adopted as verification evidence. Access credential owners
update the credential before syncing RadiusUser (`radius.sync_credential_to_radius`
and `radius.ensure_radius_users_for_subscription`); RadiusSyncJobs updates only
RadiusUser, and RadiusUsers exposes reads. No inspected writer locks RadiusUser
then AccessCredential. Five-second lock_timeout and the owner's one deadlock retry
apply. PostgreSQL T16 covers portal local reset/MFA and immediate session denial;
T17/T18 cover access and RADIUS-linked secret change, deactivation, rebind, and
source-lock blocking by exact PostgreSQL backend PID. These tests remain the
authority for deployed-schema/race evidence.

Rollout deliberately requires every legacy Redis-only customer session to sign
in again. An is_impersonation flag never exempts a payload. The existing admin
and reseller adapters retain authorization and actor audit, then obtain a normal
subscriber AuthSession through the owner before delivering impersonation Redis
state. Reset/expiry apply equally. Propagating revocation of the actor's own
session to impersonation is an existing separate follow-up, not claimed here.

No schema migration, service-secret change, local Docker, deployment, or external
operational change is part of this slice. Phase-A flushed-write detection and
MFA single-use/TOTP replay remain separately tracked work.

The session transport is fully contracted in the executable application-session
registry. Its compatibility facade converts identifiers to
CreateCustomerPortalSession/CustomerPortalSessionCreated; cache mappings convert
to CustomerSessionStandingQuery and the immutable CustomerSessionStanding result.
New Redis deliveries without committed auth_session_id are refused. The typed
standing resolver never leaks mutable ORM rows. Legacy fixture projections are
created directly through the transport store to test retirement; browser
customer fixtures now obtain real database standing through authorized test
impersonation. No unrelated portal business contracts are migrated.

Refresh policy uses CustomerSessionRefreshQuery/CustomerSessionRefreshOutcome,
capping the Redis expiry at the minimum of its sliding TTL, absolute lifetime,
and fresh database expires_at. The refresh cookie lifetime uses that capped
projection deadline. Initial delivery likewise uses database expiry. T17/T18
exercise both password and MFA completion for external sources, including
blocking of the RadiusUser row itself and exact-backend polling in autocommit.
