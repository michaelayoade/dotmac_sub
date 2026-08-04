# Customer AI Intake

**Owning service:** `crm.ai_intake`
(`app/services/crm/ai_intake.py`).

## Boundary and purpose

Customer AI intake observes an already committed inbound Inbox message and
classifies the request before the final team-only route is settled. It owns the
classification, confidence decision, bounded follow-up state, fallback choice,
and route-ready AI metadata. It does not own message ingestion, individual
agent assignment, support-ticket state, billing state, or Lead creation.

The closed intents are technical support, billing, payment confirmation,
subscription, account access, new connection, general complaint, general
enquiry, and unknown. Categories distinguish no internet, slow internet,
intermittent connection, router fault, billing issue, payment not reflected,
subscription renewal, plan change, account login, coverage, new connection,
general complaint, general enquiry, and unknown.

Department policy is deterministic after provider validation:

- technical-support categories route to `technical_support`;
- billing, payment, subscription, account, complaints and enquiries route to
  `helpdesk`;
- coverage and new-connection categories route to `sales`; and
- unknown, invalid or low-confidence results route to `fallback` after the
  configured clarification allowance is exhausted.

The model cannot supply a team identifier. `communications.team_inbox_routing`
resolves the controlled category, intent, or department through active
`TeamInboxAiRoute` rows, then a valid `department_mappings` team UUID, then the
configured fallback team. The shared owner calls the routing owner's
team-only command. That command preserves an active assignment and never
creates, deactivates, or selects an individual-agent assignment.

## Runtime configuration

`AiIntakeConfig` is the sole customer-intake control. The runtime selects the
most specific channel/account scope and reads enablement, channel, confidence
threshold, bounded follow-up settings, fallback team, escalation delay,
department mappings, custom instructions, and campaign exclusion. A channel
route with `allow_ai_routing=false` also stops the provider call and preserves
the configured channel route.

`department_mappings` entries use a controlled `department`, `intent`, or
`category` key plus `service_team_id` (or `team_id`). Custom instructions may
refine interpretation but cannot expand the strict output vocabulary. When
campaign exclusion is enabled, messages carrying campaign/ad attribution skip
automatic intake. `escalate_after_minutes` sets the persisted fallback deadline
for a sent clarification.

## Channels and ordering

The supported runtime channels are WhatsApp, Facebook Messenger, and Instagram
DM. Their verified observation processors call the shared owner after the
Inbox message commit. This guarantees provider, credential, timeout,
invalid-JSON, and configuration-read failures cannot erase or block message
ingestion. The default/channel route is applied during ingestion; the shared
owner may subsequently replace only the team route while the conversation is
unassigned.

Email is deliberately not connected. Email continues to route by addressed
mailbox; an email `AiIntakeConfig` row does not imply runtime support.

## Follow-up, failure and idempotency

Provider output uses a strict no-extra-fields schema and closed intent,
category, department, party-type, and follow-up-key enums. Confidence is
bounded from zero to one, and invalid intent/category/department combinations
fail to fallback. Customer messages pass through the existing obvious-
identifier redactor before leaving the application.

Low-confidence or unknown output sends one server-owned question selected by a
controlled key, never arbitrary model prose. Each later inbound answer is a new
observation and is reclassified with recent redacted thread context. Once the
configured follow-up count is reached, another uncertain result routes to the
fallback. The fallback deadline is stored for operational reconciliation.

`customer_ai_intake_assessments.message_id` is unique. A webhook replay finds
the existing assessment before any provider call, does not repeat the question
or sales handoff, and can idempotently re-request only the recorded team route.
Inbox webhook receipts/messages and Sales invitations retain their existing
independent uniqueness constraints.

AI disabled, missing configuration or credentials, provider timeout, invalid
JSON, unknown values, invalid confidence, missing AI route, and inactive
mapping/fallback teams all preserve the committed message. A valid configured
fallback is used where possible; otherwise the existing channel/default route
remains.

## Sales handoff

Only a high-confidence `new_connection` result with a sufficiently confident
individual/organization type is handed to `sales.lead_intake`. That owner alone
decides whether published forms and the explicit rollout gate permit an
invitation, and it alone converts a completed form to Party and Lead records.
Technical, billing, account, subscription, complaint, enquiry, unknown and
failed classifications cannot enter the Lead creation path.
