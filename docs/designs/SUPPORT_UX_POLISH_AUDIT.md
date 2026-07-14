# Support / tickets — UX-polish & operator-control audit

> **Status: historical audit evidence.** Revalidate unresolved recommendations against `docs/UI_INFORMATION_AND_ACTION_STANDARD.md` and the current domain SOT before implementation.

**Date:** 2026-06-29
**Method:** single-agent read-only review of support tickets + support-automation +
ticket settings (admin UI + services).
**Status:** implementation update applied 2026-07-01 on branch
`codex/support-ux-polish-audit`. Required P0/P1/P2 items are addressed; remaining
items are recommended enhancements rather than blockers.

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

| Tier | Status | Items |
|------|--------|-------|
| **P0** | **Done** | Merge/link now validate target UUIDs and re-render the ticket detail at 400 with an operator-facing error instead of raw 500. Service teams are no longer hardcoded/fabricated; ticket settings now expose persisted service teams, region routing rules, auto-assign enablement, and team membership. |
| **P1** | **Done** | Ticket settings now include per-priority SLA response/resolution/aging policy; ticket creation applies SLA-driven `due_at` and logs a `resolution_due` SLA event when no manual due date is set. Manual auto-assign redirects back with result messaging. Automation rule saves validate `action_value` against `action_type`. Merge now has a confirmation prompt. Regions are configurable settings. |
| **P2** | **Done** | Support ticket dates shown in admin support screens are labeled UTC. Ticket list pagination fetches `per_page + 1` so "Next" only appears when another page exists. Status colors are configurable for custom statuses. Ticket filters now use `hx-trigger="change"`. |

## Implementation update — 2026-07-01

### Done

- [x] **P0 / required:** link and merge form targets validate UUID input before
  calling service actions; invalid input returns a controlled 400 detail page.
- [x] **P0 / required:** service team options now come from
  `support_service_teams` workflow settings instead of fabricated UUID constants.
- [x] **P0 / required:** `/admin/system/ticket-settings` exposes editable service
  teams, region routing rules, auto-assign enablement, and service-team membership.
- [x] **P1 / required:** SLA policy is editable per priority; resolution targets
  drive automatic ticket due dates and create SLA events when tickets have no
  manual due date.
- [x] **P1 / required:** ticket detail/list contexts expose SLA breach/age state.
- [x] **P1 / required:** manual auto-assign result is surfaced after the action.
- [x] **P1 / required:** automation action payloads are validated by action type
  before rules save.
- [x] **P1 / required:** merge has an explicit confirm prompt.
- [x] **P1 / required:** region options are settings-backed.
- [x] **P2 / required:** support admin timestamps now include a UTC label in the
  audited screens.
- [x] **P2 / required:** ticket list pagination no longer shows "Next" on the
  last full page.
- [x] **P2 / required:** custom status colors are configurable in ticket settings.
- [x] **P2 / required:** ticket filter selects auto-submit through HTMX on change.

### Still left

- [ ] **Recommended:** replace raw UUID text boxes for link/merge with a searchable
  ticket picker. The raw-500 bug is fixed; the picker is a usability enhancement.
- [ ] **Recommended:** add a scheduled/background SLA breach materialization job
  if operators need persisted breach records beyond current due-date/SLA-state
  detection in the admin UI.
- [ ] **Recommended:** add richer team-management views if team membership grows
  beyond the compact settings form.
- [ ] **Recommended:** broaden timezone display cleanup outside the audited support
  admin screens as part of the systemic date/time pass.

## Appendix — full findings
- [CONTROL] (High) `app/services/web_support_tickets.py:85-90` — `service_team_options()` 3 hardcoded teams w/ fabricated UUIDs drive dropdowns + automation assign_team → **DONE:** settings-backed service teams; no fabricated defaults.
- [CONTROL] (High) `app/services/support.py:123-124` (no UI) — auto-assign toggle / region routing rules / team membership are real settings but no admin UI (hand-edit DB JSON only) → **DONE:** settings panel on ticket settings page.
- [CONTROL] (High/Med) `app/services/support.py` — no SLA policy: `due_at` manual, no per-priority targets, no breach/aging → **DONE:** configurable SLA targets, due-date application, SLA event logging, breach/age state in admin UI.
- [POLISH] (High) `app/web/admin/support_tickets.py:320-329` + `detail.html:163-166` — manual auto-assign discards result dict (matched/reason) → **DONE:** result message shown after action.
- [POLISH] (High) `support_tickets.py:360-375` + `web_support_tickets.py:610-625` (+ `:337-352`) — merge/link `UUID(...)` no try/except → **DONE:** invalid UUIDs return 400 re-render; ticket picker remains recommended.
- [POLISH] (Med) `templates/admin/support/tickets/detail.html:201-208` — merge (destructive) no confirm while delete has it → **DONE:** merge confirmation added.
- [CONTROL] (Med) `app/services/support.py:1713` — region list hardcoded `[north..central]` (also routing-rule keys) → **DONE:** region defaults come from settings and still include discovered ticket regions.
- [POLISH] (Med) `app/web/admin/support_automation.py:81-131` — `action_value` JSON not cross-checked vs `action_type`; mismatched saves silently no-op → **DONE:** action-type-specific validation added.
- [POLISH] (Med) templates (`detail.html:60,106,158`, `_table.html:41`, `automation/index.html:63`) — dates naive UTC, no tz label → **DONE:** audited support admin dates now label UTC.
- [POLISH] (Low) `web_support_tickets.py:772` + `_table.html:53-59` — `has_next_page = len(rows) >= per_page` shows Next on last full page; no total → **DONE:** fetches `per_page + 1`; total count remains optional.
- [CONTROL] (Low) `support_ticket_settings.py:200-214` — `status_color` hardcoded map vs configurable statuses (custom → grey) → **DONE:** status colors are editable settings.
- [POLISH] (Low) `templates/admin/support/tickets/index.html:63-87` — status/type/assignee selects lack `hx-trigger` (require Apply) → **DONE:** select filters trigger on change.
- Verified: CRUD + audit layer solid; status/priority/type lists configurable; JSON-rule automation engine.
