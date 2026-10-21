# Administrative credential commands

The `auth.credential_administration` application coordinator owns create,
password/username/metadata update, explicit deactivation, and atomic subscriber
registration metadata plus optional local login provisioning. Its flush-only
participants are `auth.credential_administration_mutation` and
`customer.registration_metadata`. Routes retain the credential-write permission
and explicit admin-role guard used by API-key minting. Actor identity comes
from the authenticated dependency; missing actor evidence fails closed.
Internal callers must supply an explicit authorized `CommandContext`.

POST `/api/v1/user-credentials` accepts subscriber identity (`person_id` remains
an input alias), optional provider, username, radius server, and plaintext
password. The configured provider is resolved before local password validation.
PATCH accepts only username, radius server, and password; omitted password is
unchanged and explicit null/blank local passwords are invalid. Plaintext passwords
are limited to 255 valid Unicode characters at both adapter and owner admission. Unknown fields,
including hashes, principal reassignment, activation/standing/counters, Party
binding, and session fields return 422, even alongside valid fields. Passwords
are omitted from repr, read outcomes, audit, events, and validation input output.
No hash-import HTTP API exists. PPPoE/RADIUS service secrets are unchanged.

Each public command requires a transaction-free caller session, including when
caller writes have already been flushed. It never completes or discards an
arbitrary caller transaction. Preparation uses a separate read session from the canonical `app.db.SessionLocal` factory, applies
existing principal-aware password policy, and hashes through the existing
`auth_flow.hash_password` before acquiring any locks. PostgreSQL locks the exact
principal before the exact credential, with a five-second per-lock timeout.
Timeout maps to 503 and `Retry-After: 1`; no command automatically retries an
unknown commit outcome and no durable replay guarantee is claimed.

Creation sets credential version 1 and, when setting a password, a marker
strictly after the current whole second to invalidate prior principal-bound
reset capabilities. Password replacement increments the version
once, clears failures/lock/must-change, preserves activation, and revokes every
active database session for the principal. Its password marker is strictly after
the current whole second so already-issued reset capabilities fail redemption,
including capabilities minted in the same second. Username changes increment
the version and revoke sessions. Changing the authentication server of a RADIUS-provider credential increments
the version and revokes sessions because the server selects authentication
authority; failure/lock counters remain unchanged. A local credential's unused
RADIUS metadata preserves standing and version. Deactivation increments the version once, revokes sessions,
and repeated deactivation is an evidence-free no-op. These operations neither
change MFA nor accept invitations.

State, database revocation, safe audit, and the distinct version-one
`credential_administration.completed` outbox event commit or roll back together.
`CredentialSessionProjectionHandler` invalidates the principal cache and repairs
portal session projections after security transitions. Until R-PR2 deploys
AuthSession-backed customer portal validation, Redis portal revocation remains
asynchronous and is **not** represented as atomic portal revocation. Failed
handler attempts remain retriable; replay is the projection repair operation.

The subscriber form submits `ProvisionSubscriberLogin`: principal validation,
manual-number prefix normalization, category/notes metadata, optional local
credential, audit, subscriber event and credential event share one owner commit.
Person/business identity is checked under the principal lock and a person login
refuses an already-active local credential. Legacy generic Subscribers.update is
not changed by this slice.

Validation uses the fast non-authoritative unit lane for behavior and atomic
rollback injection plus architecture guards. PostgreSQL integration/concurrency
acceptance requires the hosted CI disposable database migrated by Alembic; local
SQLite evidence does not establish deployed-schema or lock acceptance.

Consumer inventory at this slice found credential write consumers only in the
API, the existing-customer optional-login form, unit test helpers, and the
Playwright credential helper. The two administrative form wrappers had no
route callers. `create_subscriber_from_form` now resolves references in an
isolated read session and returns the typed provisioning outcome with explicit
actor context. Compatibility `is_active` form input remains ignored; activation
is owned by the existing account lifecycle commands. Neither wrapper was a
live route consumer at this slice. The unused `create_subscriber_from_full_form` wrapper is removed:
it composed already-committed legacy customer creation and a new flushed event
transaction with later metadata writes. Broad legacy customer creation and
other ORM seed writers remain separate migration debt; this slice claims no
atomicity for them and does not add a second transaction-completion path.


## Administrative customer MFA reset

The same coordinator owns the exact `administrative customer MFA reset` concern,
using `ResetCustomerMfaCommand` and a separate `customer:write` admission. The web
adapter takes SystemUser identity from trusted `request.state.auth`, rejects
impersonation and mismatched provenance, and opens a fresh command session. It
retains the existing customer-write support-role permission; no new admin-role
requirement or human reason field is introduced. The reason is fixed to
`Administrative customer MFA reset`. The owner validates matching active staff
identity and retains existing target eligibility: noncanceled Subscriber with
email; business targets additionally require business category and the primary
contact email represented by that Subscriber. Email-free recovery is separate.

The refreshed Subscriber FOR UPDATE precedes UUID-ordered factor locks. All
factor seeds, flags, failure counters and lockouts are cleared; active recovery
codes are deactivated. Live-factor removal or cleanup of legacy secret, counter,
lockout or recovery residue is a security transition: active database sessions
are revoked and one safe `credential_administration.completed` event is staged.
Counts distinguish live factors disabled, factor rows cleaned and recovery codes
deactivated. A fully clean repeat stages the canonical `auth.mfa_disabled` audit
only, without another event or revoking sessions created since the prior reset.
Audit, cleanup, revocation and outbox share one owner transaction and roll back
on any failure. The previous subscriber bulk writer and second wrapper audit are
retired. The original writer's factor UPDATE and first audit already shared a
commit; this migration adds complete cleanup, revocation and owner locking.

Subscriber self setup/confirmation must use the same Subscriber lock and recheck
the exact AuthSession under lock. A reset winner revokes that standing, so a
waiting enrollment cannot restore flags; an enrollment winner is subsequently
removed by reset. Acceptance of that race requires the composed enrollment slice
and hosted PostgreSQL tests in both orders. Password credential versions and reset
capabilities are unchanged; no universal MFA challenge epoch is claimed. Existing
projection handling repairs portal caches after commit while database standing
remains authoritative. Staff typed removal and native reseller flows are unchanged.
