# Party Customer Lifecycle and Attribution

**Status:** Approved architecture; additive lifecycle foundation implemented locally  
**Decision owner:** Michael  
**System of record:** Sub  
**Schema revision:** 345

## Decision

Sub owns the complete customer lifecycle. CRM has no runtime role, and
`dotmac_mkt` is not a lead, customer, attribution, or lifecycle authority.

The lifecycle is connected through stable identity rather than collapsed into
one overloaded status:

```text
interaction observation
        |
        v
reviewed Party identity --> Lead + immutable origin
                              |
                              v
                            Quote
                              |
                              v
                         Sales Order
                              |
                              v
                    Subscriber account
                              |
                              v
                         Subscription
                              |
                              v
                    Ticket / support history
```

This is not a one-way global state machine. A customer can have new Leads,
active Subscriptions, and open Tickets at the same time. Party owns who the
person or organization is; each named domain owner keeps its own lifecycle.

## Ownership map

| Fact or decision | Canonical owner | Boundary |
| --- | --- | --- |
| Person/organization identity | `party.registry` | One Party; no identity inference from one contact value |
| Inbox/social interaction | `communications.team_inbox` | Observed conversation and routing, not a sales decision |
| Native outbound campaign | `communications.campaigns` | Sub campaign, audience, recipients, and delivery state |
| Lead identity and origin | `sales.lead_lifecycle` | Party-first Lead, immutable origin, reviewed account attachment |
| Referral program | `referrals.program` | Party-first capture, reviewed account conversion, qualification and reward decision |
| Referral account orchestration | `referrals.account_conversion` | Exact Referral/Party/Lead context into atomic account creation or reviewed attachment |
| Pipeline and Quote | `sales.service` | Opportunity progress and account-specific commercial offer |
| Sales Order | `sales.orders` | Accepted/manual order and fulfilment handoff |
| Subscriber account | `customer.accounts` | Billing and service account owned by a Party |
| Customer credential enrollment | `auth.customer_credential_enrollment` | Purpose-bound local credential creation and Subscriber-email verification; no Party activation |
| Subscription state | `access.subscription_lifecycle` and catalog/subscription owners | Service lifecycle and access projection |
| Ticket lifecycle | `support.ticket_lifecycle` | Support case and official support history |
| Lifecycle convergence report | `customer.lifecycle_audit` | Aggregate read-only debt and coverage report |

## Party-first Lead contract

Before revision 355, every Lead required a Subscriber. That forced an account
row to exist before there was necessarily a customer or sale and made test,
lead, and account populations difficult to distinguish.

Revision 355 makes `Lead.party_id` the reviewed identity link and makes
`subscriber_id` optional account context. New Party-first Lead creation:

1. requires an active or quarantined reviewed Party;
2. records binding time, source, and reason;
3. does not create a Subscriber, role, contact point, or permission;
4. deduplicates open Leads by Party and pipeline; and
5. may attach a Subscriber later only when its reviewed Party matches.

An exact account-attachment retry is idempotent. A different account or Party
is refused until a reviewed merge/repoint workflow exists. Legacy
Subscriber-only Leads remain readable and auditable; revision 355 does not
pretend they have already been classified.

## Origin and attribution contract

`lead_origin_captures` records immutable, structured evidence at Lead creation.
It deliberately separates three concepts:

1. **Native Sub campaign response:** `campaign_id` and
   `campaign_recipient_id` reference Sub's communication campaign and recipient.
   The recipient Subscriber must identify the same Party as the Lead.
2. **External advertising origin:** Meta/Google campaign, ad-set, ad, form, and
   click identifiers remain provider strings. They are not coerced into Sub
   campaign UUIDs.
3. **Direct/referral/agent origin:** controlled capture method and platform,
   optional UTM fields, a path without query parameters, and explicit capture
   evidence.

Raw webhook payloads, names, emails, phone numbers, and other contact PII do not
belong in the origin row. Provider signature receipts and protected raw event
retention, if required, belong to a separate integration/security contract.

The compatibility fields `Lead.lead_source`, `Lead.campaign_id`, and
`Lead.campaign_recipient_id` are projections of the immutable capture for new
writes. External provider IDs never populate the native campaign UUID fields.
Once captured, the origin and its lead-source projection cannot be edited by a
generic Lead update.

Approved capture methods are:

- ad lead-form webhook;
- landing page;
- portal;
- agent declaration;
- native campaign response;
- referral; and
- reviewed legacy import.

Every adapter calls `sales.lead_lifecycle`; it does not write attribution
columns independently. Planned capture adapters are the Meta/Google lead-form
webhooks, public landing forms, portal/self-serve requests, Inbox campaign
responses, and the agent-reviewed Lead form.

Revision 356 activates the referral adapter contract. Referral capture creates
a quarantined Party and unverified Party contact points, then delegates Lead
creation and immutable origin to `sales.lead_lifecycle`; it does not create a
Subscriber or store capture PII in Referral metadata. Subscriber conversion
requires exact reviewed Party equality. Activation may reconcile that exact
Party link but cannot fall back to email, phone, name, or metadata. The full
contract is `docs/PARTY_FIRST_REFERRAL_CAPTURE.md`.

Referral signup and operator adjudication carry the exact PII-free
`Referral.id + referred_party_id + referred_lead_id` context through
`referrals.account_conversion`. The coordinator locks and revalidates that
triple, asks `customer.accounts` to prepare a new account when needed, then
delegates exact Party, Lead, and Referral links to their existing owners before
one commit. It never selects identity by contact value. The full boundary is
`docs/REFERRAL_ACCOUNT_CONVERSION.md`.

For the unauthenticated handoff, capture returns a 24-hour signed capability
containing only that UUID context and bounded purpose/version/time claims.
`auth.token_signing` owns the cryptographic envelope; the referral conversion
owner owns claim meaning and canonical revalidation. Public signup cannot set
account lifecycle, reseller, billing, verification, numbering, or permission
or marketing-consent state and never compares submitted contact values with
capture observations.

The subsequent credential handoff is separately owned by
`auth.customer_credential_enrollment`. No local credential exists until the
emailed 24-hour capability is redeemed with a customer-chosen password.
Completion verifies the Subscriber account email, not the quarantined Party or
its contact point, and does not change billing-block or subscription state.

## `dotmac_mkt` boundary

`dotmac_mkt` may publish social content, manage ad-platform objects, and report
provider metrics. Those are marketing transport and provider observations.
Its Post, Channel, and AdCampaign models do not move into Sub as customer
lifecycle models.

If `dotmac_mkt` later supplies provider metadata to a Lead-capture adapter, the
adapter must submit structured origin evidence to `sales.lead_lifecycle` at
Lead creation. Sub remains the decision owner. A provider conversion count is
not a person-level conversion fact; person-level attribution is derived in Sub
from the captured Lead through Quote, Sales Order, Subscriber, Subscription,
and Ticket links.

## Downstream alignment

Revision 355 adds command guards without duplicating Party onto every table:

- A Quote linked to a Lead must use a Subscriber whose Party matches the Lead.
  A legacy unbound Lead must use its exact legacy Subscriber.
- A Sales Order linked to a Quote must use the Quote's exact Subscriber.
- A Ticket may be Lead-only, which supports pre-sales questions. If it also
  links Subscriber/customer account/person rows, every linked Subscriber must
  match the Lead Party; a legacy Lead requires its exact Subscriber.
- `Subscriber.sales_order_id`, Subscription, and downstream support links stay
  with their current owners and are measured for convergence by the audit.

Quote still requires a Subscriber today. Creating or reusing the reviewed
account is therefore an explicit conversion step before an account-specific
Quote, not an accidental side effect of Lead capture.

## Subscription and billing block independence

Billing enforcement work does not conflict with this slice. Party and Lead
links answer identity and acquisition questions. Subscription status and
access restriction answer service and network-access questions.

A billing block may project a Subscription/account into blocked access without:

- changing Party identity;
- rewriting Lead, origin, Quote, or Sales Order;
- changing attribution;
- removing the customer from historical lifecycle cohorts; or
- preventing support history from remaining linked.

The lifecycle audit reports Subscription counts by the canonical status
vocabulary, including `blocked`; it does not decide or change that status.

## Legacy debt and audit

Revision 355 is additive and performs no backfill. Campaign compatibility and
Ticket-to-Lead foreign keys are installed as PostgreSQL `NOT VALID` constraints
so new writes are protected without falsely claiming historical rows are clean.
They are validated only after the audit and reviewed repair work reach zero
unresolved violations.

Run the PII-free report in a read-only, repeatable-read transaction:

```bash
python -m scripts.migration.audit_customer_lifecycle
```

The report contains aggregate counts only for:

- Party/Subscriber Lead binding coverage and mismatch debt;
- structured origin coverage and projection drift;
- Party-first referral capture, quarantined adjudication readiness, conversion,
  and legacy PII-metadata debt;
- native campaign/recipient validity;
- Quote-to-Lead and Order-to-Quote alignment;
- Subscriber-to-Sales-Order and Subscription coverage; and
- Ticket-to-Lead/account alignment.

It never binds a Party/account, infers attribution, changes a lifecycle state,
or prints identity values.

## Cutover gates

1. Deploy revisions 345 and 346 without data writes.
2. Run and retain the aggregate lifecycle audit.
3. Classify legacy Subscriber-only Leads and invalid campaign/Ticket references
   through a protected reviewed worklist.
4. Enable capture adapters one at a time and verify exact-retry/idempotency,
   Party match, provider signature, and PII-minimization behavior.
5. Shadow lifecycle/attribution reporting against the captured join chain.
6. Repair legacy debt through separately approved commands; do not infer it in
   the migration.
7. Validate deferred foreign keys only after zero unresolved violations.
8. Retire metadata-only attribution inference and any CRM/mkt bridge fallback
   after parity is proven.

Rollback before reader cutover disables new capture adapters and command use;
it does not delete captured evidence. No production migration, backfill,
adapter cutover, or deferred-constraint validation is authorized by this
document or revisions 345/346.
