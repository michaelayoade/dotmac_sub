# ADR 0006: Temporary CRM live-chat authority

- Status: retired 2026-08-30 (superseded by the retirement record at the foot
  of this file). The decision text below is preserved unedited as the record of
  what was decided on 2026-07-27; it no longer describes the running system.
- Date: 2026-07-27
- Review owner: Dotmac operations
- Retirement condition: Selfcare Team Inbox becomes the sole live-chat
  authority. MET, by decommission of the counterparty rather than by cutover.

## Context

Selfcare's native widget was activated before the operational CRM inbox
cutover. Production comparison found 70 populated native Selfcare chats and 126
inbound messages that did not exist in CRM. CRM remained the staffed
omnichannel inbox. Keeping both independent writers would continue losing
operator visibility and create an unbounded reconciliation gap.

## Decision

Until the explicit CRM-exit gate passes, CRM is the live-chat transport and
operational inbox authority for customer and reseller portal chat.

- `control.settings_spec` owns the `comms.chat_session_authority` decision.
- `crm` mode brokers an authenticated session through the typed
  `crm.chat_session.v1` capability and returns CRM REST/WebSocket endpoints.
- Selfcare does not persist or mirror CRM conversations or messages in this
  mode.
- The native visitor-message owner fails closed when authority is not
  `selfcare`, including for previously issued tokens.
- Existing Selfcare-only history is moved through a bounded, timestamp-
  preserving, idempotent import that suppresses live CRM automation.

This is a temporary, approved deviation from the target Sub-as-authority
architecture. It does not reintroduce staff credentials, a direct CRM client,
or a dual writer.

## Reversal

At final cutover:

1. reconcile and verify all CRM history required by Selfcare;
2. enable and validate native Selfcare channels and staffing;
3. set `comms.chat_session_authority=selfcare`;
4. verify new portal sessions return `/widget` and `/ws/inbox`;
5. verify the fiber site loads Sub's widget, accepts one exact-origin public
   session as `surface=fiber_website`, and receives an agent reply;
6. verify zero new `surface=customer|reseller_portal|fiber_website` CRM sessions during the
   observation window;
7. disable the `crm.chat_session.v1` binding;
8. remove this temporary capability and control in a focused follow-up.

Historical CRM rows remain audit evidence and are not copied back as live
Selfcare messages.

## Consequences

- CRM stays operationally complete during the transition.
- Authority can be switched without a code rollback.
- A missing/disabled CRM capability fails the broker request; it never silently
  falls back to native Selfcare writes.
- Imported historical CRM conversations are provenance-marked and do not have
  a live portal reply transport. Agents use them as backlog evidence and follow
  up through an active CRM channel.

## Verification

- focused broker tests prove both authority modes and zero native writes in CRM
  mode;
- a native-token test proves the hard write gate;
- connector manifest tests pin CRM 1.0.0 and 1.1.0 during adoption;
- importer tests prove timestamp preservation, identity failure closure, and
  idempotent replay;
- the operator runbook requires source/target count parity and a replay with
  zero creates.

## Retirement, 2026-08-30

The retirement condition is met, but not the way this ADR anticipated. The CRM
was decommissioned on 2026-08-29 -- containers removed, `/opt/dotmac_omni/`
deleted, and `crm.dotmac.io` now resolving to an unrelated host presenting a
certificate for a different common name. There is no counterparty left to hold
live-chat authority, and none is coming back.

### What was removed

The reversal steps in this ADR assumed a live CRM that could be reconciled
against and then switched away from. Steps 1, 2, 4, 5 and 6 are therefore
unexecutable as written. What was executed instead is the end state they aimed
at, in code:

- `comms.chat_session_authority` -- the spec is deleted and migration
  `569_retire_crm_chat_authority` deletes any surviving row.
- `app/services/chat_session_authority.py` and
  `app/services/crm_chat_session.py` -- deleted.
- `crm.chat_session.v1` -- removed from the current `dotmac.crm` manifest
  (new version 1.2.0). Manifest 1.1.0 is retained byte-identical as a
  historical pin because a published digest is immutable and an installation
  adopts by digest; the runner maps no action to the capability any more, so a
  1.1.0-pinned binding fails closed with `capability_not_supported`. Migration
  569 additionally DISABLES any `crm.chat_session.v1` capability binding
  without deleting it -- see "The history gap" below for why that row is not
  expendable.
- `CRMClient.create_widget_session` and the `crm_capability` facade method --
  deleted, and the widget-session exemption is removed from
  `tests/architecture/test_no_crm_writeback.py`.
- `POST /webhooks/crm/chat` and the `message.outbound` event -- deleted. Its
  only job was waking a mobile device for a conversation Sub did not hold.

### What was deliberately NOT removed

- Every `InboxConversation`, `InboxMessage`, `integration_inbox` receipt,
  `integration_config_revisions` row and pre-existing `domain_setting_history`
  row. Those are business and audit records.
- The `crm.chat_session.v1` capability-binding row itself, which is disabled
  rather than deleted. Because the chat capability ran on the INTERACTIVE
  path, it produced no delivery and no inbox receipts, so that row's
  `enabled_at` / `disabled_at` / `created_by` is the ONLY receipt production
  holds for it.
- The `dotmac.crm` connector's other capabilities (ticket and subscriber
  observation, portal session, quote command) and `app/api/crm.py`, the inbound
  API Sub serves. They point at the same dead host but are separate
  integrations with separate retirement slices; see the SOT relationship map.

### The invariant that outlives the decision

The operator runbook closed with "Do not enable two chat writers or add a
fallback that writes locally when CRM is unavailable." That sentence is now
`tests/architecture/test_single_chat_authority.py`, which fails the build if a
second broker destination, a chat-authority setting, a retired module path or
an external chat-transport capability reappears -- and which carries its own
sensitivity proof, so it cannot pass by finding nothing to check.

### The history gap this ADR predicted

Step 1 of the runbook's rollback -- "Stop and investigate any unresolved
CRM-to-Selfcare history gap" -- cannot be discharged from the repository, and
after the CRM's deletion without a final backup it may not be dischargeable at
all. Nothing in Sub records whether the write barrier was ever engaged: the
exporter is read-only and stamps nothing, the importer lived in the CRM,
`inbox_conversations` and `inbox_messages` carry no provenance column, no
migration or seed ever wrote the authority row, and the barrier itself raised
a `TeamInboxWidgetError` that produced a 503 and no durable record.

That is why migration 569 records the setting's value into
`domain_setting_history` before deleting it and disables the capability binding
instead of removing it. `docs/runbooks/TEMPORARY_CRM_CHAT_AUTHORITY.md` is
retained, marked retired, and carries the exact production queries that can
still settle the question.
