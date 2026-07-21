# Referral account conversion

## Decision

`referrals.account_conversion` owns the cross-domain command that carries a
Party-first referral into Subscriber account creation or reviewed attachment.
It does not become the owner of Party identity, Subscriber account state, Lead
links, Referral state, subscription state, billing, or access.

The stable conversion context is the existing PII-free UUID triple:

```text
Referral.id + Referral.referred_party_id + Referral.referred_lead_id
```

Every adapter must carry all three values in a typed command. The coordinator
locks the Referral, Party, Lead, and any selected existing Subscriber, then
compares the submitted identifiers with canonical state before any write. A
stale or altered value is refused. Email, phone, name, address, UTM, metadata,
or other contact/marketing values never select a Party or account.

No schema revision is required: migrations 244 and 356 already store the
Subscriber, Lead, Party, and Referral context and evidence fields needed to
prove conversion. Adding a parallel context table would duplicate authority
and create a new drift path.

## Owner calls

Each public command enters `execute_owner_command` on a transaction-free
session. The coordinator performs one transaction in this order:

1. lock and validate the exact Referral/Party/Lead context;
2. ask `customer.accounts` to prepare a new Subscriber, or lock the exact
   existing Subscriber selected by an operator;
3. ask `party.registry` to bind that account to the exact referred Party;
4. ask `referrals.program` to attach the Subscriber, which delegates the Lead
   account link to `sales.lead_lifecycle`;
5. stage `subscriber.created`, `referral_account.converted`, and PII-free audit
   evidence for a new conversion, then commit once.

The coordinator owns the root business transaction; collaborators never
commit, roll back, or open a savepoint. A refused self-referral, stale context,
Party conflict, or late audit/event failure rolls back the account and every
binding. Commands are idempotent: an exact retry returns the already attached
Subscriber without another audit row or conversion event, while a different
Subscriber or Party is refused.

## Signup and operator surfaces

Public capture returns a signed capability containing only the exact Referral,
Party, and Lead UUIDs plus purpose/version/timestamps. The referral owner
defines the claims and resolves the bounded lifetime only from
`subscriber.referral_signup_context_expiry_minutes` through
`control.settings_spec`, where its only default and min/max bounds are declared.
`auth.token_signing` owns the configured JWT key/algorithm and cryptographic
envelope. Signature, expiry, purpose, version, subject, UUID shape, and maximum
lifetime are all verified before canonical rows are locked and revalidated.
Rotation of the auth signing key invalidates outstanding capabilities. The
token is a sensitive bearer value: adapters must not log it, persist it as
business data, place it in URLs, or copy it into Referral/Lead metadata.

`POST /referrals/signup` accepts that bearer capability and a narrow account
payload. The public schema permits basic name, contact, address, locale, and
timezone fields only. It forbids caller-selected lifecycle status, reseller,
verification, billing, numbering, authorization, marketing-consent, and
existing-person controls. Public accounts enter the existing account owner
with requested status `new`; the lifecycle owner derives the resulting state.

The capability establishes continuity with the exact capture; it is not
identity verification. Submitted signup email and phone are deliberately not
compared with captured contact observations. An exact replay returns the
already attached account instead of creating another Subscriber. The Party
remains quarantined until a separate identity decision activates or merges it.

Account creation does not invent a password or create a disabled/placeholder
credential. The account owner command returns transaction-free; the public
adapter then asks
`auth.customer_credential_enrollment` to send a separate purpose-bound
capability to the account email. Delivery failure is reported without rolling
back or duplicating the already committed account. The adapter does not commit
or release a transaction between the two owners. Redemption creates the
customer-chosen local credential and verifies the Subscriber email atomically. See
`docs/REFERRAL_CREDENTIAL_ENROLLMENT.md`.

The staff API exposes two thin adapters:

- create a Subscriber from an explicit `SubscriberCreate` payload plus the
  stable referral context; and
- attach an existing Subscriber after exact-Party review.

The create adapter requires both referral write and customer-create authority.
The attach adapter requires referral write and customer-update authority. The
admin referral detail page exposes the attach action with hidden Party/Lead
context and an explicit review reason. The server revalidates every value; the
hidden fields are not trusted.

Subscriber compatibility fields come only from the explicit account payload.
They are never copied automatically from quarantined Party contact points. The
public and staff creation adapters call the same owner; neither may fall back
to matching the submitted email or phone.

## Runtime policy and invariants

The capability expiry is operator-tunable policy and therefore has one
database-authoritative setting/resolver. Adapters and the conversion owner do
not repeat its default or bounds and do not read an environment fallback at
runtime. The token
purpose, issuer, claim allowlist, schema version, maximum token field size, and
clock-skew check remain code-level protocol/security invariants. The set of
Referral lifecycle states eligible for conversion is versioned domain policy,
not an adapter option and not a transport hardcode.

## Quarantine and lifecycle boundary

A quarantined Party may receive a reviewed Subscriber binding because
quarantine describes identity confidence, not account status. Conversion does
not activate, merge, archive, or repoint the Party. It also assigns no Party
role or permission.

The requested Subscriber account status remains owned by
`access.subscription_lifecycle`. The conversion coordinator neither clears nor
overrides blocked, suspended, disabled, delinquent, subscription, billing, or
network-access facts. Focused tests prove that a requested blocked account
remains blocked after conversion.

Credential enrollment observes the same boundary. An account that becomes
`blocked` between signup and redemption remains blocked; enrollment writes no
status or lifecycle-override field.

The PII-free customer lifecycle audit separates quarantined referrals awaiting
operator adjudication from active Parties awaiting ordinary account creation.
It reports aggregate counts only and performs no repair.

This document authorizes no production deployment, migration, account
creation, identity decision, Party activation, merge, backfill, or cleanup.
