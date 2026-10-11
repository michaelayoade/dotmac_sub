# Bounded Subscriber verification delivery cutover

`auth.email_verification` owns the typed request and flush-only participant.
Authenticated API, customer and Subscriber-backed reseller resend adapters open
an independent `owner_command_session` after deriving the authorized target.
Staff and first-class reseller-user API principals are refused. The supplied
caller session is never committed, released or rolled back by this path.
Public request row locks use PostgreSQL transaction-local `lock_timeout=5s`.
A timeout rolls back only the independent owner and maps to HTTP503 with
`Retry-After: 5`; the caller may still complete its pending/flushed transaction.
Requests carry a required typed `AuditActor` from the authenticated context;
profile audit and its verification participant use the same actor. Customer
and reseller impersonation sessions lack trusted actual-actor provenance, so
resend and customer profile updates now refuse them with HTTP403. API resend
also refuses impersonation. This observable restriction remains until actual
actor provenance is propagated securely through those sessions.

The public request locks and reloads exactly one Subscriber UUID. Portal profile
email changes stage the participant inside the existing profile owner transaction.
AuditEvent, communication intent, recipient decision and Notification outbox commit
or roll back together. Submission is flush-only; no token, renderer or SMTP call
runs before commit. Delivery means queued, not transport accepted.

The recipient is resolved by `identity.capability_recipient`, supplied explicitly
for email only, with reseller expansion disabled and identity inference disabled.
Current account policy still applies. Immediate transactional delivery uses the
existing queue owner's after-commit wake-up and worker; periodic processing
remains recovery. The intent's unique dedupe key is derived from stable request
context. A fingerprint binds Subscriber UUID, normalized email digest, bounded TTL,
purpose and schema version; reuse with changed evidence fails closed. Exact-row
locking serializes same-Subscriber requests. Cross-Subscriber key races are
arbitrated by the existing database unique constraint and roll back the loser.
The returned planner intent is fingerprint-checked again to reject a competing
commit observed by the planner's own dedupe read. Exact unique-index conflicts
become typed idempotency refusals; the losing owner stages no request audit.

The allowlisted `auth.email_verification` descriptor contains only Subscriber UUID,
email digest, TTL and purpose. Request metadata also carries request/correlation
UUIDs and fingerprint. Intent and Notification subject/body are null. The worker
checks envelope version, exact keys, UUID/audience/recipient digest, current email,
active/unverified account and purpose before minting. Blocked/suspended accounts
remain eligible. The renderer returns ephemeral content only. The existing worker
uses `track=False` and `sensitive_content=True` and owns outcome persistence.

## Retired profile compatibility writer

Exact app/script consumer inventory found no product caller of
`web_customer_actions.update_customer_profile`; its eight callers existed only
in five compatibility test files. That dead writer, its separate command and
ORM-returning outcome types, and its conversion property are removed. Shared
helpers and the module's other business operations remain. Product profile
writes use `customer_portal_profile_commands` directly with an explicit trusted
`AuditActor`; migrated tests do the same on fresh Sessions inside fast-unit
fixture isolation. The test session helper owns lifecycle only and never derives
an actor or releases the caller transaction. Its evidence is non-authoritative
and does not replace hosted PostgreSQL acceptance. An architecture guard rejects
restoring the legacy definitions, imports, alias calls or dynamic alias fallback.

## Atomic email capability completion

`CompleteEmailVerificationCommand` hides its token in repr and returns only UUID
and boolean outcome values. The owner decodes the existing signed capability
inside its transaction, validates purpose, Subscriber principal type/UUID and
email proof, then locks and reloads that exact Subscriber with PostgreSQL
`lock_timeout=5s`. It compares the current canonical single email address to the
exact token claim under the lock before writing `email_verified`. Expiry is
rechecked after lock acquisition, including before an already-verified no-op. A repeated
proof for the same current verified address succeeds without another audit.
Changed addresses and malformed/expired capabilities receive a generic refusal.
The flag and canonical AuditEvent commit or roll back together; audit failure
cannot leave the account verified. Audit identifies the Subscriber through
`AuditActor.user` using the verified capability proof, consistent with credential
recovery. This represents capability possession, not password authentication.
Audit metadata contains only proof kind and request UUIDs, never email or bearer.

API and web completion adapters open independent owner Sessions, preserving any
caller pending/flushed transaction. Invalid proofs map to generic API401/web400;
operational failures map to503 with `Retry-After: 5`. The old
`auth_flow.verify_email` writer had only these two product callers and is retired;
compatibility tests now exercise the typed canonical owner. Its legacy decoder
HTTP401 is translated to a safe capability exception before crossing the owner
boundary. Other decoder/storage failures remain operational failures.

## Explicit remaining debt

This bounded request/completion cutover does not complete all S7 work.
Mobile `/me` still calls legacy `auth_flow.set_subscriber_email`; its partially
flushed profile transaction must migrate through a reviewed profile coordinator.
`auth_flow.send_email_verification` and the low-level send compatibility wrapper
remain solely for that legacy flow and existing compatibility tests. They must
not be reused by new adapters. No caller transaction may be rolled back merely
to fit a new owner boundary.

The query-token URL and verification GET route remain unchanged. The GET mutation
needs a real browser bridge before a fragment URL cutover. Completion locking and
audit are migrated; mobile sending and browser handoff remain separate slices.

Focused unit tests provide fast behavioral evidence only. Hosted PostgreSQL tests
must run against the real Alembic chain to prove exact locking, bounded caller
self-block preservation, same-request convergence, owner-level cross-Subscriber
conflict rollback, direct concurrent unique constraint arbitration, and completion
waiting on an exact Subscriber lock before rejecting an intervening email change
or a capability expiring while waiting. No local Docker or database acceptance is claimed.
