# Temporary CRM chat authority runbook — RETIRED 2026-08-30

**This procedure is retired and must not be run.** It is retained because it is
the only description of what the CRM live-chat arrangement was, and because its
rollback step 1 — "Stop and investigate any unresolved CRM-to-Selfcare history
gap" — is the one step that was never dischargeable and still is not. The
original text is preserved unedited at the foot of this file.

`crm.dotmac.io` was decommissioned on 2026-08-29 and now resolves to an
unrelated host presenting a certificate for a different common name. Every
target, precondition and capability this runbook depends on is gone.

## What replaced it

Nothing switched; the arrangement was removed. Sub's native Team Inbox
(`communications.team_inbox_widget`) is the sole live-chat authority for the
customer portal, the reseller portal and the public fiber site. The
`comms.chat_session_authority` selector, the `crm.chat_session.v1` capability,
the CRM broker, `CRMClient.create_widget_session` and the inbound
`POST /webhooks/crm/chat` receiver are deleted. See
`docs/adr/0006-temporary-crm-chat-authority.md` § "Retirement, 2026-08-30" and
`docs/CHAT_LIVE_SETUP.md`.

The invariant this runbook closed with — "Do not enable two chat writers or add
a fallback that writes locally when CRM is unavailable" — now lives as
`tests/architecture/test_single_chat_authority.py`, not as a sentence.

## The unresolved history question

**Whether the cut to CRM was ever executed cannot be determined from the
repository.** Stated plainly, because the answer decides whether any customer
conversation is missing.

What the repository establishes:

- The capability was built, tested and merged on 2026-07-27, and the code sat
  unchanged until its removal on 2026-08-30.
- `scripts/one_off/export_native_chat_for_crm.py` is strictly read-only. It
  writes a mode-0600 file and stamps nothing back into Sub, and the runbook
  required both copies of that file to be destroyed afterwards. Running it left
  no trace.
- The importer, `scripts/import_selfcare_chat_history.py`, lived in the CRM
  repository and wrote only to the CRM database. It is gone with the CRM.
- `inbox_conversations` and `inbox_messages` carry **no** provenance,
  `source_system`, `external_id` or origin column. There is no marker that can
  separate a CRM-era conversation from a native-era one.
- No migration, seed or script in this repository ever wrote the
  `comms.chat_session_authority` row. Setting it to `crm` could only have been
  a manual operator action, through
  `PUT /api/v1/settings/comms/chat_session_authority` or direct SQL.
- The write barrier wrote nothing durable. `_require_enabled` raised a
  `TeamInboxWidgetError`, which the adapter mapped to HTTP 503. No audit row,
  no metric, no counter.
- No commit message, `CHANGELOG.md` entry, audit document or ledger entry
  anywhere in the repository records the cutover as having been *performed* —
  only as shipped. This team does produce dated execution records for other
  cutovers, so the absence is informative but is not proof.
- The 2026-08-29 sweep of Sub's dependencies on the dying CRM found and fixed
  two things (the portal quote money path and the NCC weekly report) and did
  not mention live chat.
- Native fiber-site chat shipped 2026-08-09 behind the same fail-closed
  authority gate. If it has been working since, authority was `selfcare` from
  at least that date.

What follows: it is **likely** that production authority was `selfcare`
throughout, or at minimum from 2026-08-09. It is **not proven**, and the
repository cannot distinguish "never executed" from "executed in late July and
rolled back before 2026-08-09".

**If the cut was executed, any conversation that lived only in CRM after the
barrier is unrecoverable.** The CRM was deleted without a final backup — a
deliberate decision, not an accident.

### The queries that can still settle it

Run these against Sub production. Queries 1-3 and 6 read rows that migration
`569_retire_crm_chat_authority` changes, so run them **before** deploying 569 if
you want the raw pre-migration state. 569 preserves the evidence either way: it
records the setting's value into `domain_setting_history` before deleting the
row, and it disables the capability binding rather than removing it.

```sql
-- 1. The live authority value. Note: value_text, not "value". Do not filter on
--    scope; the row may sit at tenant or platform scope.
SELECT id, tenant_id, scope_kind, scope_id, value_text, value_json,
       value_type, is_active, created_at, updated_at
  FROM domain_settings
 WHERE domain = 'comms' AND key = 'chat_session_authority';
-- Zero rows  => the setting was never written; resolution always fell through
--               to the spec default 'selfcare' and the cut was never made.
-- updated_at > created_at => the value changed at least once after creation.

-- 2. The attributed change log. CAVEAT: domain_setting_history was created by
--    migration 520 on 2026-08-11 and was NOT backfilled, so a flip made on
--    2026-07-27 does not appear here. An empty result is not exoneration. A row
--    with value_before='crm' proves a rollback. After 569, a row with
--    action='delete' carries the value the setting held at retirement.
SELECT changed_at, action, value_before, value_after,
       changed_by_party_id, change_reason, request_id, ip_address
  FROM domain_setting_history
 WHERE domain = 'comms' AND key = 'chat_session_authority'
 ORDER BY changed_at;

-- 3. The capability binding. A non-null enabled_at is the closest thing to
--    direct proof the cutover's preconditions were satisfied in production.
--    The chat capability ran on the interactive path, so it produced no
--    integration_deliveries and no integration_inbox receipts: this row is the
--    only receipt. 569 disables it and preserves these timestamps.
SELECT b.id, b.capability_id, b.state, b.enabled_at, b.disabled_at,
       b.created_by, b.updated_by, b.created_at, b.updated_at,
       i.connector_key, i.connector_version, i.state AS installation_state,
       i.manifest_digest
  FROM integration_capability_bindings b
  JOIN integration_installations i ON i.id = b.installation_id
 WHERE b.capability_id = 'crm.chat_session.v1';

-- 4. When an operator prepared the cutover. Config revisions are append-only
--    and 569 does not touch them, so this survives regardless.
SELECT r.revision, r.created_at, r.created_by, r.validation_status,
       r.config_json ->> 'base_url'              AS base_url,
       r.config_json ->> 'chat_widget_config_id' AS chat_widget_config_id,
       r.config_json ->> 'chat_ws_url'           AS chat_ws_url
  FROM integration_config_revisions r
  JOIN integration_installations i ON i.id = r.installation_id
 WHERE i.connector_key = 'dotmac.crm'
 ORDER BY r.revision;

-- 5. The behavioural fingerprint, and the one query that needs no control row
--    at all. The barrier blocked add_visitor_message, so a genuine CRM-authority
--    window MUST appear as a hard zero-inbound-message gap beginning on the day
--    of the flip. Continuous traffic across late July and August is conclusive
--    proof the barrier was never set.
SELECT date_trunc('day', m.created_at)   AS day,
       count(*)                          AS inbound_widget_messages,
       count(DISTINCT m.conversation_id) AS conversations
  FROM inbox_messages m
 WHERE m.channel_type = 'chat_widget'
   AND m.direction    = 'inbound'
   AND m.created_at  >= TIMESTAMPTZ '2026-07-01'
 GROUP BY 1
 ORDER BY 1;

-- 6. The manual operator action, if it went through the API. This predates
--    domain_setting_history and is the only source that can catch a late-July
--    flip. A miss means either it never happened or it was done by direct SQL.
SELECT occurred_at, actor_label, actor_id, actor_party_id, action,
       entity_type, status_code, is_success, ip_address, request_id, metadata
  FROM audit_events
 WHERE entity_type LIKE '%chat_session_authority%'
 ORDER BY occurred_at;
```

---

# Preserved original procedure — do not run


## Scope and targets

This runbook applies to `selfcare.dotmac.io`, `crm.dotmac.io`, and the widget
embedded on `fiber.dotmac.ng`. It moves live-chat authority without dual
writing and reconciles native history before the final Sub cutover.

## Preconditions

- CRM deployment containing `scripts/import_selfcare_chat_history.py`;
- Sub deployment containing `crm.chat_session.v1` and the native hard gate;
- the existing `dotmac.crm` installation adopted to manifest 1.1.0;
- `chat_widget_config_id` points to the active `DotMac Self-care` CRM widget;
- `crm.chat_session.v1` is bound, statically validated, connection-validated,
  and enabled;
- `CHAT_LIVE_ENABLED=true`;
- no message bodies or private exports are printed to logs.

## Cut to CRM

1. Record current Selfcare populated-conversation and inbound-message counts.
2. Create a preflight export in an operator-owned mode-0600 file outside the
   repository and transfer it to a mode-0600 path on `crm.dotmac.io`.
3. Run the CRM importer in dry-run mode. Stop on an unmapped or ambiguous
   identity, count mismatch, digest mismatch, or existing-content conflict.
4. Set `comms.chat_session_authority=crm`. This is the write barrier: existing
   native tokens now fail closed and all new portal sessions use CRM.
5. Create and transfer a new final export after the barrier. Dry-run that exact
   file; do not apply the preflight file. This final snapshot closes the race
   where a native message could arrive during preflight.
6. Open a customer portal chat and verify the broker returns the CRM origin and
   CRM WebSocket. Send one controlled message and verify it appears once in CRM
   and zero times in Selfcare.
7. Apply the CRM history import.
8. Replay the importer. Require zero created conversations/messages and all
   source items reported reused.
9. Compare source and target provenance counts.
10. Remove both private exports from both hosts.

## Roll back to Selfcare

1. Stop and investigate any unresolved CRM-to-Selfcare history gap.
2. Set `comms.chat_session_authority=selfcare`.
3. Open a controlled portal chat and verify `/widget` plus `/ws/inbox`.
4. Configure the fiber site with Sub's public base URL, open a controlled
   fiber-site chat, and verify it appears once as `channel_type=chat_widget`,
   `surface=fiber_website`; verify an agent reply reaches that browser session.
5. Verify one native message appears once in Selfcare and no new CRM portal or
   fiber-site
   session is created.
6. Observe traffic and delivery health.
7. Disable `crm.chat_session.v1` after the observation gate passes.

Changing the authority setting is the rollback. Do not enable two chat writers
or add a fallback that writes locally when CRM is unavailable.
