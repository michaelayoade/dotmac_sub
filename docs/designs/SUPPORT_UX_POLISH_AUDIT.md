# Support / tickets — UX-polish & operator-control audit

**Date:** 2026-06-29
**Method:** single-agent read-only review of support tickets + support-automation +
ticket settings (admin UI + services).
**Status:** audit only. Part of the remaining-module audit series.

## What this audit is

Two tracks (definition in `NETWORKING_UX_POLISH_AUDIT.md`): **POLISH** and
**CONTROL**. Support's signature gap is that its **routing/automation primitives
exist only as hand-edited DB JSON** — real settings the engine reads, with no admin
UI — i.e. dead controls for anyone who isn't an engineer.

## Acceptance criteria (support-specific)

1. Ticket routing (teams, regions, auto-assign rules, membership) is editable from
   an admin UI, not raw DB JSON; team identifiers are real, not fabricated.
2. SLA/aging is a configurable policy with breach detection, not a per-ticket manual
   `due_at`.
3. Destructive/state-change actions (merge, link, delete) validate input (no raw
   500), confirm, and show a result.
4. Every automation rule that saves either acts or is rejected — no silent no-ops.

## Cross-cutting themes

### POLISH

**P-A. Crash / missing validation.** Merge and link take a free-text
`target_ticket_id`/`to_ticket_id` and call `UUID(...)` with no try/except — a blank
or non-UUID value raises `ValueError` → **raw 500** (`app/web/admin/support_tickets.py:360-375,337-352`,
`web_support_tickets.py:610-625`). Unlike `ticket_create`, there's no
validation-to-400 path. → reuse `_ticket_form_error`; ideally a ticket picker.

**P-B. No-visible-result / silent no-ops.**
- "Manual Auto-Assign" returns a result dict (`matched`/`reason`) but the route
  discards it and plain-redirects — no toast on whether/why anything assigned
  (`support_tickets.py:320-329`)
- Automation `action_value` is a raw JSON textarea never cross-checked against
  `action_type`; a mismatched value saves cleanly and silently no-ops at apply
  (`app/web/admin/support_automation.py:81-131`)

**P-C. Confirms.** Merge (moves comments/attachments/links, closes source) has no
`confirm()` though Delete does (`templates/admin/support/tickets/detail.html:201-208`).

**P-D. Display / pagination.** All dates naive UTC `strftime`, no tz label
(systemic); `has_next_page = len(rows) >= per_page` shows "Next" on the last full
page, no total count (`web_support_tickets.py:772`); index filter selects lack
`hx-trigger` (require Apply, inconsistent with auto-submit search).

### CONTROL

**C-1. Routing primitives hardcoded / UI-less (the signature).**
- `service_team_options()` returns **3 hardcoded teams with fabricated UUIDs** that
  drive create/edit/detail dropdowns *and* are what automation `assign_team` rules
  must reference — operators can't add/rename teams or learn the real UUIDs
  (`app/services/web_support_tickets.py:85-90`)
- Auto-assign toggle (`support_ticket_auto_assign_enabled`), region routing rules
  (`support_region_assignment_rules`), team membership
  (`support_service_team_members`) are real settings the engine reads but have
  **zero admin UI** — editable only by hand-editing DB JSON (`app/services/support.py:123-124`)
- Region list hardcoded `["north","south","east","west","central"]` (also the keys
  routing rules must match) (`support.py:1713`)

**C-2. No SLA/aging policy.** `due_at` is fully manual per ticket; `TicketSlaEvents`
is only a log; no per-priority response/resolution target, no breach detection, no
aging threshold (`app/services/support.py`). → configurable per-priority SLA +
aging, drive `due_at`/breach from them.

**C-3. status_color hardcoded** vs operator-configurable statuses — any custom
status falls back to grey with no way to assign a color (`support_ticket_settings.py:200-214`).

## Priority

| Tier | Items |
|------|-------|
| **P0** | Merge/link free-text UUID → raw 500 (P-A); teams hardcoded with fabricated UUIDs + auto-assign/routing/membership settings have no UI — the routing feature is effectively unusable by operators (C-1) |
| **P1** | SLA/aging policy (C-2); manual auto-assign result surfacing + automation action_value validation (P-B); merge confirm (P-C); region set as setting (C-1) |
| **P2** | tz display, pagination next/total (P-D), status_color (C-3), filter `hx-trigger` |

## Appendix — full findings
- [CONTROL] (High) `app/services/web_support_tickets.py:85-90` — `service_team_options()` 3 hardcoded teams w/ fabricated UUIDs drive dropdowns + automation assign_team → back with `service_teams` table/setting, expose in admin [recommend]
- [CONTROL] (High) `app/services/support.py:123-124` (no UI) — auto-assign toggle / region routing rules / team membership are real settings but no admin UI (hand-edit DB JSON only) → settings panel on the ticket-settings page (`system.py:3998`) [recommend]
- [CONTROL] (High/Med) `app/services/support.py` — no SLA policy: `due_at` manual, no per-priority targets, no breach/aging → configurable SLA + aging, drive due_at/breach [recommend]
- [POLISH] (High) `app/web/admin/support_tickets.py:320-329` + `detail.html:163-166` — manual auto-assign discards result dict (matched/reason) → silent reload → surface as toast/flash [recommend]
- [POLISH] (High) `support_tickets.py:360-375` + `web_support_tickets.py:610-625` (+ `:337-352`) — merge/link `UUID(...)` no try/except → raw 500 → 400 re-render / ticket picker [recommend]
- [POLISH] (Med) `templates/admin/support/tickets/detail.html:201-208` — merge (destructive) no confirm while delete has it → add confirm [recommend]
- [CONTROL] (Med) `app/services/support.py:1713` — region list hardcoded `[north..central]` (also routing-rule keys) → make region set a setting [defer]
- [POLISH] (Med) `app/web/admin/support_automation.py:81-131` — `action_value` JSON not cross-checked vs `action_type`; mismatched saves silently no-op → validate shape per action_type on save [recommend]
- [POLISH] (Med) templates (`detail.html:60,106,158`, `_table.html:41`, `automation/index.html:63`) — dates naive UTC, no tz label → convert/label [defer]
- [POLISH] (Low) `web_support_tickets.py:772` + `_table.html:53-59` — `has_next_page = len(rows) >= per_page` shows Next on last full page; no total → fetch per_page+1 or show total [defer]
- [CONTROL] (Low) `support_ticket_settings.py:200-214` — `status_color` hardcoded map vs configurable statuses (custom → grey) → derive color in settings or accept grey explicitly [defer]
- [POLISH] (Low) `templates/admin/support/tickets/index.html:63-87` — status/type/assignee selects lack `hx-trigger` (require Apply) → add `hx-trigger="change"` [defer]
- Verified: CRUD + audit layer solid; status/priority/type lists configurable; JSON-rule automation engine.
