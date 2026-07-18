# Referral customer credential enrollment

## Decision

`auth.customer_credential_enrollment` owns local credential enrollment for a
Subscriber created through the signed public referral flow. Account creation
and credential creation are separate commands:

```text
signed referral signup
        |
        v
Subscriber committed (`new`, no credential)
        |
        v
non-secret enrollment delivery intent
        |
        v
worker revalidates context and mints capability in memory
        |
        v
customer chooses password
        |
        v
local UserCredential + Subscriber.email_verified (one commit)
```

The owner never generates, stores, emails, or logs a plaintext placeholder
password. A failed or suppressed delivery leaves the committed Subscriber
intact. The request response reports whether delivery was queued or suppressed;
the durable notification outcome provides retry and final delivery state.

This slice requires no migration. `UserCredential` and
`Subscriber.email_verified` are existing canonical fields.

## Capability contract

The enrollment capability is signed by `auth.token_signing`, is minted only
when the delivery worker is ready to invoke the email transport, and expires
24 hours after that materialization. It contains only:

- purpose, issuer, version, subject, issued-at, and expiry claims;
- the exact Referral, Party, Lead, and Subscriber UUIDs; and
- a SHA-256 digest of the normalized account email.

It contains no email address, name, phone, password, contact value, consent, or
lifecycle state. Redemption verifies the signature, expiry, purpose, issuer,
version, subject, UUID shape, maximum lifetime, exact canonical
Referral/Party/Lead/Subscriber relationship, and current email digest.

The capability is a sensitive bearer value. It may appear only in the HTTPS
self-care action URL fragment delivered to the account email and in the
customer's redemption request body. Browser fragments are not sent in the GET
request, access log, or normal referrer header; the self-care page moves it to
the POST form in-browser. It is never written to a communication intent,
Notification body or metadata, delivery response/error, audit event,
Referral/Lead metadata, application log, or credential table. Rotation of the
configured auth signing key invalidates outstanding capabilities.

Successful redemption is single-use because the owner locks the canonical
Subscriber and refuses any existing local credential. Concurrent attempts are
serialized by that row lock; the local username uniqueness constraint is the
final collision guard. A replay cannot replace the chosen password.

## Credential and verification consequence

The public completion payload accepts only the bearer token, a
customer-chosen password, and an optional username. The existing dynamic
password minimum applies, the password is capped at the model/API maximum, and
only its one-way hash is stored. Usernames are normalized to lowercase and
checked case-insensitively before the database constraint is applied. When no
username is supplied, the account email is the proposed login username; a
shared-email collision requires a different explicit username.

Credential insertion and `Subscriber.email_verified = true` are one
transaction. Email verification here means possession of the Subscriber
account email that received the capability. It does not:

- mark a `PartyContactPoint` verified;
- activate, merge, archive, or repoint a Party;
- resolve duplicate identity evidence;
- change Subscriber status or lifecycle overrides;
- change Subscription, billing-block, network-access, or support state; or
- grant a Party role, RBAC role, permission, consent, or marketing opt-in.

A canceled, disabled, or inactive account refuses enrollment. Other states,
including `new` and `blocked`, are preserved because their owners remain
independent.

## Delivery boundary

`communications.eligibility` remains the only decision owner for whether the
account email may receive the transactional `credentials` message. A
marketing-only unsubscribe does not suppress it; an all-scope hard bounce,
complaint, or erasure does.

`auth.customer_credential_enrollment` submits a normal transactional
communication intent containing only a typed action version, exact canonical
Referral/Party/Lead/Subscriber UUIDs, and the normalized email digest. Neither
the intent nor its Notification contains a token or rendered body.

After the normal queue policy and delivery-time eligibility gates,
`communications.ephemeral_actions` validates the allowlisted action envelope
and delegates the domain-specific context revalidation. The auth owner refuses
a changed recipient, stale relationship, terminal account, or existing local
credential; otherwise it mints and renders the capability in memory. The email
transport receives sensitive content with durable content/error persistence
disabled while retaining Notification and NotificationDelivery outcome state.
Failures use bounded, secret-free retry state. Each retry revalidates context
and mints at transport time, so capability lifetime does not burn down while a
message waits in the queue.

The previous immediate, untracked email deviation is retired. SMTP and the
configured self-care domain remain transports and do not decide enrollment,
verification, identity, account, or subscription state. Billing-blocked and
suspended accounts remain eligible for this actionable `credentials` category;
canceled, disabled, or inactive accounts fail closed.

## Public adapters and failure behavior

- `POST /referrals/signup` commits the exact referral account first, then
  requests enrollment delivery. Its response distinguishes `queued`,
  `suppressed`, `rate_limited`, `already_enrolled`, and
  `manual_review_required`. Token expiry is absent at request time because no
  token exists until the worker materializes the message.
- `GET/POST /portal/auth/credential-enrollment` is the self-care HTML form on
  the configured self-care domain. The JSON equivalent is
  `POST /api/v1/auth/credential-enrollment`. Both are unauthenticated because
  the signed capability is the narrow authority. Neither accepts Subscriber,
  Party, lifecycle, verification, role, or permission fields from the caller.
- Delivery requests are limited to three per Subscriber per 15 minutes.
- An inactive pre-existing local credential requires manual review; public
  signup never reactivates or replaces it.
- A changed account email, stale canonical relationship, canceled account,
  expired/tampered token, replay, or username collision fails closed.

Focused tests cover the non-secret queued envelope, just-in-time token lifetime,
PII-free claim set, absence of placeholder credentials and persisted bearer
values, secret-free transport errors, retry rematerialization, suppression,
exact canonical context, pre-delivery drift, tamper/expiry/email-change failure,
replay protection, password hashing, username collision rollback, public route
guards, Party quarantine, Party contact verification, and billing-block
independence.

This document authorizes no production deployment, migration, account change,
credential creation, email send, Party decision, or subscription-state change.
