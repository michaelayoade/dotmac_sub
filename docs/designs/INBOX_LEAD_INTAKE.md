# Inbox Lead Intake

**Owning service:** `sales.lead_intake`
(`app/services/sales/lead_intake.py`).

## Purpose

Lead intake turns a final qualifying classification for an unknown prospect in
a supported Meta Inbox conversation into a Party-first Lead. Lead creation does
not depend on sending or completing a form. The form is optional enrichment for
identity and service-location details. Neither path creates a Subscriber,
activates service, or grants marketing consent.

The owner controls three concerns:

1. immutable, versioned individual and organization form templates;
2. atomic materialization of a final classified sales candidate into Party,
   Lead, exact Inbox participant binding, conversation-to-Lead provenance,
   optional Sales routing, internal note, audit evidence, and `lead.created`;
3. optional invitation eligibility, expiry, delivery, and revocation; and
4. optional form enrichment of the same Party and Lead, including
   `lead.updated`, while retaining legacy manual-form conversion.

`ai.intake` owns general customer classification, confidence, clarification,
fallback, and team selection. Inbox receivers, the AI gateway, public/admin
routes, templates, event handlers, and Meta delivery workers are adapters. They cannot create
or update the Sales-owned records directly.

## Classified-candidate contract

When `ai.intake` persists a final, no-follow-up `new_connection` or
`coverage_request` classification for a WhatsApp, Facebook Messenger, or
Instagram DM message with a known individual/organization type, it stages
`ai.intake_lead_candidate_classified` in the same transaction. The event uses a
message-derived deterministic ID and contains only the typed classification,
operator tenant ID, message/conversation IDs, provider/model labels, and
allowlisted PII-free Meta referral fields. The Sales handler validates the
tenant before entering its owner; a cross-tenant event is a permanent,
reviewable refusal. Meta referral data is acquisition evidence, not a
standalone Lead decision.

The durable Sales handler enters `sales.lead_intake` once. At the configured AI
confidence threshold (or the conservative default when no channel config is
present), that owner deterministically creates the provisional Party, prospect
role, scoped contact point, immutable `inbox_classification` / `team_inbox`
Lead origin, active conversation-to-Lead link, and audit/event evidence. Exact
replay returns the same Lead and link. A published form template can add routing
defaults but is not required for materialization.

## Optional invitation eligibility and rollout

Automatic form invitation is fail-closed. It occurs only when all of the following
are true:

- `integration.lead_intake_auto_send_enabled` is explicitly enabled;
- exactly one individual and one organization template version are published;
- an active channel-specific or `any` `AiIntakeConfig` exists;
- the conversation is active, unresolved (`unmatched`), and has no Subscriber;
- the channel is WhatsApp, Facebook Messenger, or Instagram DM;
- the shared customer-intake owner hands off `new_connection` or
  `coverage_request` at or above the configured confidence threshold; and
- the customer type is individual or organization at the same threshold.

General ambiguity and provider/schema failure are handled before Sales by
`ai.intake`. These rules control only the optional invitation. Disabling
automatic sends or lacking templates never suppresses classified Lead
materialization.

The database permits only one automatic invitation per conversation. Staff
with `crm:lead:write` may issue an invitation manually, revoke an active link,
and then issue a replacement.

## Template and token contract

Templates are drafts until published. Publishing retires the previous version
for that customer type, and published/retired versions are immutable so an
issued invitation always resolves to the copy and routing policy reviewed at
issuance. The invitation message must contain `{link}`.

Public links use a cryptographically random token. Only its SHA-256 digest is
stored. Tokens expire after the configured duration, bounded to 24 hours, and
completed or revoked tokens are rejected. Public responses are non-cacheable,
use no-referrer policy, require CSRF validation, reject unknown fields, and are
rate-limited by a hash of client address and token digest.

## Submission contract

Individual forms require full name, gender, date of birth, service address,
address confirmation, and privacy acknowledgement. Organization forms require
organization name, representative name, representative role, business/service
address, address confirmation, and privacy acknowledgement.

The browser supplies only selected coordinates. On save, the server reverse
geocodes them, requires country `NG`, and normalizes the state or FCT through
the canonical Nigerian-state normalizer. Submitted identity data is not logged
or copied into AI assessment rows.

When the invitation already references a classified Lead, completion enriches
that same Party/Lead and retains its immutable `inbox_classification` origin.
It does not create a second Lead. For a legacy manual invitation with no
provisional Lead, completion still performs the original atomic conversion:

- creates a Person Party, or an Organization Party plus representative Person
  and `contact_for` relationship;
- adds the prospect role and exact channel contact point with unknown consent;
- creates a Party-first Lead with immutable `inbox_form` / `team_inbox` origin
  only for the legacy no-provisional-Lead path;
- binds only the provider-scoped Inbox participant endpoint that received the
  link;
- routes the conversation to the template's Sales service team;
- adds a PII-free internal note, audit evidence, and `lead.created` or
  `lead.updated` event; and
- marks the invitation completed and links the created records.

The owner never creates a Subscriber. Account conversion remains with
`sales.account_conversion` through the established Quote acceptance path.

## Delivery and repair

Invitation and completion messages use the canonical Team Inbox reply command.
WhatsApp uses its configured sender; Facebook Messenger and Instagram DM use
the provider account scope captured on the inbound message and the durable
notification queue. Delivery records never store the public token separately
from the outbound message body already required for transport.

Operators diagnose drift by comparing final qualifying classification metadata,
the durable candidate event, active Lead link, immutable Lead origin, exact
participant binding, optional invitation link, and Lead event/audit record.
Replaying the candidate consequence or a completed form command returns the same
deterministic Party and Lead identities. Historical repair is report-first and
must re-enter this owner; operators must not manually insert Lead/link rows.

The repair CLI defaults to a PII-free, read-only 60-day preview:

```bash
python scripts/support/reconcile_inbox_classified_leads.py --days 60
```

Its `--apply` mode suppresses invitation delivery and re-enters the same Sales
owner for each finding. Staging or production apply requires explicit operator
approval, plus `--actor` and `--reason`; the normal release and database backup
controls still apply.

## Schema

Migration `470_inbox_lead_intake` adds the Sales-owned template, assessment, and
invitation records. Migration `629_inbox_classified_lead_origin` additively
allows the immutable `inbox_classification` capture method and requires its
platform to be `team_inbox`. General intake state remains in the canonical Team
Inbox metadata written by `ai.intake`; no competing classification table is
created.
