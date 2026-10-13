# Customer web/native login parity

`auth.customer_authentication` owns read-only customer password-step preparation.
The customer browser form and the customer-only JSON API use the same typed
`PrepareCustomerLogin` and `PreparedCustomerLogin` boundary. The API does not
enter the general staff/reseller credential lookup. Customer username and safe
email alias decisions remain with `auth.customer_login_identity`.

The extracted policy preserves the browser's local-first order, exact password
bytes, customer lockout settings, email-alias refusal (no hopping to network
credentials after a wrong email password), bounded RADIUS attempts and the
existing direct access-secret decrypt-and-compare compatibility. It snapshots
RADIUS/access standing before external verification and never substitutes a
post-verification source as evidence. No user/service format policy, credential
representation, stored username, password, or network protocol is changed.

Web preparation retains the existing general browser audience and cookie/Redis
delivery. Native preparation requires `UserType.customer` and stamps the new
`customer` audience on verified evidence. `auth.password_authentication` remains
the sole session/challenge and failure-counter writer. Its principal eligibility
gate rechecks customer classification inside the success transaction. Native MFA
requires the same bound customer audience; generic, staff, vendor and reseller
challenges cannot be exchanged through the customer endpoint. Reset, revocation,
source-version locking and challenge standing are retained.
Customer native MFA keeps the portal's TOTP-only verification, code trimming,
zero clock window and shared MFA failure/lockout policy. The general API's
recovery-code behavior remains unchanged.

The JSON endpoints are `POST /api/v1/customer/auth/login` and
`POST /api/v1/customer/auth/mfa/verify`. Login accepts username/password only;
source/provider choice is server-owned. Browser forms continue using
`/portal/auth/login`. Other portals and `/api/v1/auth/login` remain compatible.
The new login is included in the existing credential-stuffing throttle and
excluded from the generic API sync-pressure bucket.

The customer app uses those endpoints. An existing unsupported cached/profile
identity is discarded through the existing session-wipe coordinator.
An old cache without account classification waits for a fresh customer profile;
an unavailable server preserves its credentials without rendering that cache.
Login or MFA success followed by a failed profile read is reported separately and can
retry the profile without repeating password or MFA verification. Requests have
a stable UUID request ID across transport replay; diagnostic breadcrumbs include
only method, redacted path, status and request ID. Shared policy denial logs
contain correlation, bounded reason/surface and target kind, never entered
identifier, password, hash, tokens or raw headers.

Unit parity tests are non-authoritative fast-lane evidence. PostgreSQL tests
using the real migration chain must prove native source-standing/reset behavior.
The backend must be deployed and accepted before the new mobile build: the app
does not silently fall back to the general login endpoint on 404. No deployment,
production credential change or broad rollback of previous auth protections is
part of local implementation.
