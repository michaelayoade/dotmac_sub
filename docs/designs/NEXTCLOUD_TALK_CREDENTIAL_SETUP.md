# Installed Nextcloud Talk credential setup

`integration.installations` owns this transition in
`nextcloud_talk_installation.py`; `secrets.reference_store` owns external secret
transport. This editor configures an existing approved notifier account and an
existing `nextcloud.talk` installation. It never creates a Nextcloud account.

Page contract: `/admin/integrations/nextcloud-talk/{installation_id}` is an admin
editor for operators maintaining notifier credentials. The typed read projection
shows the approved server, persisted installation state, notifier username and
whether an app-password reference is configured. Reference presence does not
prove current credential availability. GET requires settings read; POST requires
both settings write and secrets write, and the existing admin CSRF middleware.
The Installed integrations Manage action links here. One primary action saves
and validates. Consequences appear before submission; Recent Activity provides
progressive evidence. Permission gates use the canonical wildcard-aware `can`.
No password or technical secret reference appears in the projection or HTML.

The frozen command carries CommandContext, installation UUID, expected revision
and manifest pin, operation UUID, username and a repr-hidden SecretStr password.
The approved `url`, timeout, manifest, binding scope and policy are preserved.
Exactly one existing Talk message binding is required, preventing generic
activation from enabling unrelated capabilities. Retired/quarantined, unsupported
pins, stale revisions and different current operations fail before external I/O.
No root/admin credential input or arbitrary URL/path/reference is accepted.

All installation configuration/lifecycle/binding writers lock the parent row
before mutation. The first acquisition refreshes stale identity-map state under
no-autoflush; repeated helpers retain the same lock and their intentional pending
writes until transaction completion. Parent locks precede binding locks. Hosted
PostgreSQL tests must prove cross-session ordering; SQLite unit tests are logic
evidence only.

The secret transport derives a UUID5 scope from trusted OpenBao namespace and the
persisted installation environment. The immutable path is
`integrations/nextcloud-talk-credentials/<scope>/<installation UUID>/<operation UUID>`.
Only the typed creator writes the `app_password` field plus a nonsecret configuration fingerprint using KV2 CAS=0. The fingerprint binds approved configuration, manifest, original references, actor/scope, expected revision/installation, and binding scope/policy so changed same-operation configuration also conflicts after a SQL rollback.
Generic secret edit/delete reject this reserved prefix and canonicalized aliases.
One bounded readback verifies exact bytes with a constant-time comparison.
Denied/unavailable/malformed responses return a fixed safe error with a durable
failure audit/event and no installation configuration change. No credentials are
logged, echoed through framework form validation, stored in SQL/outbox, or
captured in credential-route telemetry events.

After confirmed creation/readback, one disabled immutable revision is staged.
The existing Talk runtime validates the exact binding using GET room listing;
no staff test, message, account provisioning or global notification setting is
changed. Success enables the intended singleton binding. Failure commits the
candidate disabled with safe audit/event evidence. An exact current-operation
retry verifies the password and replays the committed outcome; changed username
or password conflicts, and older operations cannot resurrect old revisions.
Validation failure returns a fresh form operation UUID so corrected credentials
can create a new version. Storage ambiguity retains the same UUID for retry.

OpenBao and SQL are not one atomic transaction. A successful external create
followed by SQL commit failure can leave an immutable orphan. Retry the same
operation UUID and exact credentials to recover; otherwise request safe owner
review. Never overwrite/delete automatically, and never enqueue a password for
background retry. Existing secret references remain readable.

## Operator rollout dependency

The application currently may have read-only access to the existing credential.
This UI needs a separately authorized, one-time narrow OpenBao grant for read and
create on the new immutable data prefix. No update/delete/list/sudo grant is
required; KV2 CAS0 must remain enforced by the application and effective policy.
This source change does not grant live capabilities or copy production/personal
credentials. A denied deployment reports storage unavailable safely.

If an existing enabled Talk installation has a missing credential binding, the
unchanged candidate preflight correctly blocks deployment. The legitimate
bootstrap is: keep global Talk delivery off; use an authenticated installation
owner command to disable only the broken Talk installation; deploy the exact
candidate with Talk explicitly inactive and unaccepted; obtain the separately
approved narrow OpenBao grant; enter the notifier app password through this
admin UI and save/validate; rerun intended-enabled candidate checks and staging
acceptance on the same digest. Do not waive gates, seed dummy material, rebuild
an image solely for credential changes, or claim inactive Talk acceptance.

Required rollout evidence: focused behavior/redaction/permission/render tests,
architecture guards, and the full hosted CI suite. Actual PostgreSQL concurrency
must use the migration-owned disposable hosted database. No local Docker or
SQLite result establishes deployed-schema or concurrency acceptance.
