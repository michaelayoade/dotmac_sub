# Projection-secret fetch atomicity

Owner: `access.projection_secret_fetch`. This is Sub's disarmed ADR-0076
section 7 bridge, consumed by the authenticated Integrator projection port.

The plan author opens an authorization in its existing transaction. The row
contains only the request reference, credential identity, version and expiry.
It contains no secret. The authenticated API adapter requires the dedicated
`integration:projection-secret:reveal` machine scope and supplies a typed
command carrying that principal's identity. A fresh owner-command session
keeps authentication reads outside the business transaction.

Redemption locks and refreshes the credential first and the request second.
It holds both locks through owner completion, serializing credential rotation
with release and concurrent redemptions with the request's burn. Active state,
binding, version and expiry are checked after the lock waits. A retained ORM
identity never substitutes for current locked state.

Decryption occurs only in memory. The owner stages `redeemed_at` and one
pending `access_projection_secret.revealed` outbox event in the same
transaction, then commits before returning a typed result whose secret is
excluded from its representation. The normal durable dispatcher delivers the
event; redemption does not install an after-commit dispatcher callback.
Event payloads contain identifiers and version only, never secret material.

Validation, decryption, event staging and other pre-commit failures roll back
the burn and event. They release no secret. The earlier registry prose saying
decryption failure spends the reference contradicted both the implementation
and its reveal-event contract; this change preserves successful-release-only
burn semantics and corrects that prose. An uncertain commit result can leave
the burn and event durable without returning cleartext. A spent reference is
never replayable; the plan owner issues a fresh authorization when replanning.
The event is an owner-side committed reveal fact, not a receipt from the
connector or evidence of provider activation.

Credential refusals remain one opaque 409. Owner-boundary failures produce a
bounded unavailable response. Neither response echoes submitted material.
The response's representation also excludes the secret, while the authenticated
wire response carries the secret required by the fetch contract.

The PostgreSQL canaries use the real Alembic chain and independent connections.
They prove one release, one burn and one event under concurrent redemption;
credential rotation ordering; stale identity refresh; expiry crossing an exact
observed lock wait; and pre-commit rollback. SQLite tests are fast unit evidence
only. Hosted CI owns execution evidence.

Both network capability publication and outbound provisioning flags remain
off. This source change performs no live fetch, credential binding, shadow
qualification, `TEST_RADIUS_*` retirement or writer cutover.
