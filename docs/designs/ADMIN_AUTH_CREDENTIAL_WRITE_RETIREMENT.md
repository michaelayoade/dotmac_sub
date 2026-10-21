# Administrative authentication credential-write retirement

Q-N1 removes generic session and MFA creation/update. The actual API mount is
`/api/v1`; `/api/v1/auth/sessions` in the discovery register was shorthand.
`POST /api/v1/sessions`, `PATCH /api/v1/sessions/{session_id}`,
`POST /api/v1/mfa-methods`, and `PATCH /api/v1/mfa-methods/{method_id}` are retired.
Their service methods and input schemas are removed too, preventing a parallel
adapter from retaining the same capability. No replacement impersonation flow
is introduced. Existing rows need no migration.

Authentication owns session issuance (`app.services.auth_flow.stage_session_issue`,
called by the password authentication owner); renewal remains
`app_sessions.refresh`. The authenticated MFA setup/confirmation flows in
`app.services.auth_flow.AuthFlow` generate secrets on the server, persist pending
methods disabled, and require proof before activation. Recovery remains with
its existing authentication owners. This slice removes parallel writers; it
introduces no new command owner and does not change those owner's contracts.
Administrative GET/list and DELETE revoke/reset adapters remain available.

## Consumer inventory

Repository-wide searches covered `app`, `tests`, `scripts`, `mobile`,
`field_mobile`, templates and documentation. `app/api/auth.py` was the only
runtime consumer of `Sessions.create/update`, `MFAMethods.create/update`, and
the four retired Pydantic input models. Unit consumers in
`tests/test_auth_services.py` and `tests/test_auth_helpers.py` are migrated to
explicit non-authoritative ORM fixture setup for retained read/revoke tests;
tests of retired create/update behavior are removed. No client migration is
required for mobile, field mobile, tasks or scripts. The OpenAPI snapshot is
regenerated to declare the intentional removal.

Password-hash administration is deferred to a separate consumer migration:
`app/services/web_subscriber_forms.py:create_subscriber_with_optional_login` creates local
login credentials with `UserCredentialCreate` and
`auth_service.user_credentials.create`; `tests/playwright/helpers/auth.py`
posts a password hash to `/api/v1/user-credentials` to provision test users.
Removing that API without migrating those consumers would break existing work.

## Enforcement and validation

`tests/architecture/test_admin_auth_write_retirement.py` exercises actual route
dispatch for all four removed operations (405 even without a parent auth guard),
pins retained GET/DELETE contracts, and refuses service/schema resurrection.
Existing authentication/MFA tests verify server-owned setup, confirmation,
login and recovery. SQLite unit fixtures prove behavior only; they are not
migration or PostgreSQL acceptance evidence. No schema change is involved.
