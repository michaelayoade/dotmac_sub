# Party-first referral capture

## Decision

`referrals.program` owns Refer & Earn capture policy, the canonical ReferralCode
and Referral records, the Referral-to-Subscriber attachment record,
qualification and reward policy, and atomic program-transition orchestration.
`referrals.account_conversion` owns the cross-domain conversion command. It
uses `party.registry` for identity and reachability facts,
`sales.lead_lifecycle` for Lead identity and immutable origin, Subscriber
services for accounts, and `financial.credit_notes` for reward issuance. CRM is
not a decision owner or runtime participant.

Customer API and web referral reads/writes call the native owner
unconditionally. The obsolete referral read/write controls, CRM client
`create_portal_referral` mutation, mirror write-through, and scheduled outbound
mirror reconciliation are retired. Tombstone tasks retain the old Celery names
only to absorb queued messages without database or network work. The legacy
mirror is a read-only historical comparison/retention view. The signed legacy
referral webhook route is a 200/no-op tombstone so queued deliveries stop
retrying; it performs no delivery claim, parse-dependent decision, mirror or
native write, notification, credit, or outbound call. Referral support is also
removed from the CRM-to-native delta adapter.

Revision 356 removes the capture-time fake-account pattern. A public or portal
referral submission now creates, in one transaction:

1. one quarantined Person Party whose display name is the submitted name or the
   neutral label `Referred prospect`;
2. unverified, unknown-consent Party contact points for the submitted email
   and/or phone;
3. one Party-first Lead created by `sales.lead_lifecycle`;
4. one immutable Lead origin with method/platform `referral` and normalized
   Lead source `Referrer`; and
5. one pending Referral bound to the same Party and Lead.

It does not create a Subscriber, role, permission, subscription, billing
record, support record, or reward. Contact PII is not copied into new Referral
metadata or Lead origin. The contact point is the canonical reachability
observation; the Party remains quarantined because a form submission is not
reviewed identity proof.

## Contact matching boundary

Email and phone are not identity keys. Capture may use the existing identity
resolver only as a conservative guard to reject a known self-referral or known
active customer. It does not reuse that Subscriber or Party.

The same referral code plus the same normalized submitted contact set may be
recognized as an HTTP/form retry. This is request deduplication only: it does
not merge Parties, attach accounts, or deduplicate people across referrers.
Shared or ambiguous contact values therefore never silently establish who a
person is.

## Command and transaction boundary

Referral-code issuance, public capture, customer refer-a-friend, activation
qualification, operator qualification, rejection, and reward issuance are
typed commands. Each enters `execute_owner_command` once on a
transaction-free adapter session. The owner locks the canonical row before a
check-then-write decision:

- code issuance locks the exact Subscriber;
- capture locks the exact active ReferralCode before comparing a retry;
- lifecycle transitions lock the Referral before Subscriber or financial
  account state; and
- the database code constraint arbitrates the vanishingly rare generated-code
  collision.

Party, Lead, Referral, credit-note, audit, and domain-event changes commit or
roll back together. Exact replays return the canonical outcome without another
Party, Lead, Referral, credit, audit row, or transition event. Events and audit
metadata contain only canonical identifiers, state, bounded financial
evidence, and command tracing; submitted contact/name/address/notes and the
shareable referral code are excluded.

## Reviewed account conversion

`referrals.account_conversion` is the account-conversion command owner. Its
transaction-neutral `Referrals.attach_subscriber_for_conversion` collaborator
writes the canonical Referral attachment record and requires:

- a Referral with complete Party-binding evidence;
- a Subscriber with its own reviewed Party binding;
- exact equality between the referred Party and Subscriber Party;
- a different Subscriber/Party from the referrer;
- the attributed Lead; and
- non-empty conversion source and reason.

The command delegates the Lead account link to `sales.lead_lifecycle`, records
Referral account-link evidence, is idempotent for an exact retry, refuses a
different target, and does not commit its caller's transaction.

`referrals.account_conversion` is the approved coordinator for account
creation and operator attachment. It carries the exact Referral/Party/Lead UUID
triple, locks and revalidates it, asks `customer.accounts` to prepare a new
Subscriber when required, then calls the existing Party and Referral owners in
one transaction. Subscriber fields come only from an explicit account payload;
Party contact points are never copied or matched automatically. See
`docs/REFERRAL_ACCOUNT_CONVERSION.md`.

Public capture also mints a PII-free signed continuation containing that exact
UUID triple. Public signup verifies the signed envelope through
`auth.token_signing`, resolves its bounded lifetime only through
`subscriber.referral_signup_context_expiry_minutes`, revalidates canonical
Referral state, and accepts only a narrow account payload with no status,
reseller, billing, verification, numbering, authorization, or
marketing-consent controls. The capability is continuity evidence, not contact
or identity verification; the resulting Party stays quarantined.

Signup then requests a separate credential-enrollment email from
`auth.customer_credential_enrollment`. The account is already committed before
delivery, and no generated/placeholder password is stored. Redemption creates
the local credential and verifies only `Subscriber.email`; Party quarantine,
Party contact verification, roles, consent, subscription state, and billing
blocks are unchanged. The detailed boundary is
`docs/REFERRAL_CREDENTIAL_ENROLLMENT.md`.

Subscriber activation may reconcile an unattached pending referral only when
the activated Subscriber already has the exact referred Party. It never falls
back to name, email, phone, metadata, or a newly created account.

An admin qualification override may bypass activation/window policy, but it
cannot bypass Party-first account conversion. The reviewed Subscriber must be
attached first; reward qualification cannot turn an unconverted contact into a
customer fact.

## Reward and communication consequence

`financial.credit_notes` remains the money owner. Reward issuance uses its
transaction-neutral preview, account lock, idempotency reservation, issued
CreditNote, audit, and funding-ledger evidence. The exact historical
`referral:<UUID>` reference remains the idempotency namespace, so replay can
repair a Referral whose legacy credit was already issued without posting money
again. A normal new payout emits `referral.reward_issued`; recovery of existing
financial evidence emits `referral.reward_reconciled` and deliberately does not
send another customer message.

The reward owner never calls push or another delivery SDK. The versioned event
flows through `communications.event_policy`, whose canonical editable template
and channel policy create a deduplicated communication intent per event and
channel. Transport retries therefore cannot repeat the reward decision or
create a second intent.

## Runtime policy

Program enablement, reward amount/currency, qualification window,
auto-approval, and share-base URL resolve only through `control.settings_spec`.
Their defaults and bounds are declared only in the settings registry;
`referrals.program`, API/web adapters, and documentation do not repeat runtime
fallbacks. The code alphabet/length, field-size limits, exact lifecycle
transition set, and event schema version remain protocol/model invariants.

## Legacy compatibility and rollout

Revision 356 is additive. Existing Subscriber-only referrals and legacy
`metadata.capture` remain readable; the migration does not backfill or infer
their Party. The old partial unique account guard remains alongside the new
active-referred-Party guard.

Before deploying the new capture path in an environment:

1. deploy revisions 339 through 356 in order;
2. verify new captures create no Subscriber rows and always have Party, Lead,
   binding evidence, and immutable referral origin;
3. verify exact-Party conversion and mismatched-Party refusal;
4. verify activation qualifies exact account links and never contact matches;
5. verify command replay, transaction rollback, legacy reward-credit recovery,
   and deduplicated event notification evidence;
6. measure legacy Subscriber-only and PII-in-metadata debt with the PII-free
   customer lifecycle audit; and
7. plan any legacy cleanup as a separate reviewed, receipt-producing change.

This document authorizes no production migration, backfill,
deployment, identity merge, or legacy metadata deletion.
