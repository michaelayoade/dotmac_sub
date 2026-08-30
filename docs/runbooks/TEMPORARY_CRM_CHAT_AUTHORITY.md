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

## The history question — CLOSED BY DECISION, not by evidence

Whether the cut to CRM was ever executed **cannot** be determined from this
repository, and it will not be determined at all. Michael closed the question
on **2026-08-30**:

> Then CRM restoration and forensic queries are unnecessary. Remove both from
> the queue.
>
> Treat the data migration as complete and preserve a cutover receipt
> containing the final sync watermark/time and any existing target-side counts
> or digest evidence. Do not reconnect to or restore CRM merely to recreate
> evidence; if recorded retrospectively, label it as such.

Consequences, stated plainly so nobody reopens this:

- The six forensic SQL queries that previously stood in this section are
  **withdrawn**. Do not run them and do not reinstate them.
- The surviving off-host CRM backups **must not be restored** to recreate
  evidence. Restoration for this purpose is prohibited, not merely
  discouraged.
- The data migration is treated as **complete** by decision. The receipt below
  is the durable artefact; it is retrospective, and it is weaker than a receipt
  written by the barrier that moved authority.

Migration `569_retire_crm_chat_authority` still preserves what evidence exists,
because destroying it would be a separate mistake: it records the setting's
value into `domain_setting_history` before deleting the row, and it **disables**
the `crm.chat_session.v1` capability binding rather than removing it, keeping
`enabled_at` intact.

### Why Sub could not answer it — the lesson, not pending work

This analysis is retained because it is the motivating evidence for Governance
**ADR 0018**. It describes a gap in how Sub recorded authority transfer; it does
not describe an open investigation.

- The capability was built, tested and merged on 2026-07-27 (`91150bb1e`,
  #1638), and the code sat unchanged until its removal on 2026-08-30.
- `scripts/one_off/export_native_chat_for_crm.py` was strictly read-only. It
  wrote a mode-0600 file and stamped nothing back into Sub, and the runbook
  required both copies of that file to be destroyed afterwards. Running it left
  no trace. **This is the missing watermark.**
- The importer, `scripts/import_selfcare_chat_history.py`, lived in the CRM
  repository and wrote only to the CRM database. It is gone with the CRM.
- `inbox_conversations` and `inbox_messages` carry **no** provenance,
  `source_system`, `external_id` or origin column. There is no marker that can
  separate a CRM-era conversation from a native-era one.
- No migration, seed or script in this repository ever wrote the
  `comms.chat_session_authority` row. Setting it to `crm` could only have been
  a manual operator action.
- The write barrier wrote nothing durable. `_require_enabled` raised a
  `TeamInboxWidgetError`, which the adapter mapped to HTTP 503. No audit row,
  no metric, no counter. **This is the missing runtime observation.**
- No commit message, `CHANGELOG.md` entry, audit document or ledger entry
  anywhere in the repository records the cutover as having been *performed* —
  only as shipped.
- Native fiber-site chat shipped 2026-08-09 behind the same fail-closed
  authority gate.

The generalised lesson ADR 0018 draws from this: a cutover that moves authority
must emit a receipt **at the moment it moves**, naming old owner, new owner,
exact revisions, effective time, runtime observation, rollback boundary and
old-writer retirement status. Sub emitted none of these, which is precisely why
the question became unanswerable.

## Cutover receipt — RETROSPECTIVE

> **RETROSPECTIVE RECEIPT.** Reconstructed from repository state on 2026-08-30,
> **not** written by the barrier that moved authority. ADR 0018 rule 1 defines
> "immutable" as written *by* the cutover rather than composed afterwards, so
> this is weaker than a conformant receipt by construction and must never be
> cited as one. Fields that cannot be filled from already-recorded evidence are
> marked `unavailable`; none was inferred, and production was not queried to
> complete it.

Recorded in the vocabulary of Governance **ADR 0018**, *Authority cutovers
leave receipts and decommissions retire delegations* (`Accepted` 2026-08-30),
rule 1. That record post-dates the revision this repository pins in
`.dotmac/standards-profile.json`, so it is cited here by name and status rather
than as a pinned conformance obligation.

**Which tier this is.** ADR 0018 § 3 defines three: the **product** tier holds
the local evidence and does not move, the **Governance registry** holds the
non-sensitive envelope that outlives both parties, and Knowledge is discovery
support only. This receipt is the **product tier**. The Governance envelope is
Governance's to write and is not created here — a product repository composing
its own entry in a cross-repository registry it does not own would be the
hostage arrangement § 3 exists to prevent.

### The seven fields

| # | Field | Value |
|---|---|---|
| 1 | `old_owner` | `dotmac_sub`'s `comms.chat_session_authority` setting as selector, delegating to the external `dotmac.crm` system via capability `crm.chat_session.v1`. **Resource:** the decision "where a portal live-chat visitor message is written", and the rows it produced in `inbox_conversations` / `inbox_messages`. |
| 2 | `new_owner` | `dotmac_sub` service `communications.team_inbox_widget`, reached unconditionally with no selector. **Resource:** the same decision and the same two tables. |
| 3 | `revisions` | **Before:** commit `91150bb1e` (#1638, 2026-07-27), which introduced the delegation. **Switch:** Alembic revision `569_retire_crm_chat_authority`, plus connector manifest `dotmac.crm` 1.1.0 → 1.2.0 — digests `e1de51fcf0e93869ce8776c6291f8b1ac4b0a35b373adcaa322c46e5c3f48908` (1.1.0, retained byte-identical as a historical pin) and `16c79c1f244a33ac3977a650ca6cc6217d53a32634f4ea2713eb297560a0f623` (1.2.0). **After:** the squash commit of this pull request, which does not exist while this file is being written and is therefore `unavailable` here rather than guessed; ADR 0013 § 3 forbids naming a branch or "current `main`" in its place. |
| 4 | `effective_time` | `unavailable`. Rule 1 requires the instant recorded **by the transaction that moved authority**; no transaction recorded one. The retirement lands on 2026-08-30, but that is a deploy date, which the rule names as explicitly not this field. The instant authority originally moved *to* the CRM, if it ever did, is likewise unrecorded. |
| 5 | `runtime_observation` | `unavailable`, in all three of the parts rule 1 names. (a) The barrier wrote no attributed record: `_require_enabled` raised a `TeamInboxWidgetError` mapped to HTTP 503 — a response to one caller, not a fact about the system. (b) `inbox_conversations` and `inbox_messages` carry no provenance discriminator, so "which era is this row from" is unanswerable. (c) Non-engagement was never recorded either, so "the barrier never fired" and "nobody looked" are indistinguishable. The single artefact the capability ever produced is `enabled_at` on the `crm.chat_session.v1` binding, which migration `569` preserves by **disabling** the binding rather than deleting it. |
| 6 | `rollback_boundary` | **Closed and irreversible.** The CRM was decommissioned 2026-08-29 and deleted without a final backup. Surviving off-host backups must not be restored to recreate evidence (see the ruling above). There is no path back to CRM-held chat, and no reversal decision remains to be owned. |
| 7 | `old_writer_retirement` | Two displaced writers, each with an explicit rule 2 disposition. **RETIRED** — the live-chat writer, by Alembic revision `569_retire_crm_chat_authority` and the removal of `app/services/crm_chat_session.py` and `app/services/chat_session_authority.py` in this change. **STILL LIVE** — `crm_ticket_pull`, gated by `CRM_TICKET_PULL_ENABLED`, a five-minute poller that still creates `support_tickets` rows and stamps `crm_subscriber_id` onto subscriber rows. Retirement condition: the CRM ticket-observation slice, which disables the flag at its configuration owner, observes at least two poll intervals with no CRM call and no new CRM-derived write, then deletes the poller and its task registration. Owner: Michael Ayoade. |

**A STILL LIVE item blocks a decommission declaration.** ADR 0018 rule 2 is
explicit about this, so nothing in this repository may describe the CRM as
decommissioned while field 7 carries that entry — the live-chat authority is
retired, the *system* is not.

### Product-tier notes that are not rule 1 fields

Recorded because they are true and useful, and kept below the table because
adding fields to a seven-field receipt makes it harder to check, not more
complete.

- **Final sync watermark:** `unavailable`. `scripts/one_off/export_native_chat_for_crm.py`
  stamped nothing back into Sub and the runbook required both copies of its
  output to be destroyed. Running it left no trace.
- **Target-side counts:** `unavailable`. The target was the CRM database,
  deleted without a final backup.
- **Data loss:** treated as **none by decision, not by verification**. If the
  cut was executed, any conversation that lived only in the CRM after the
  barrier is unrecoverable and will remain so.

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
