# Admin UI Redesign — Archetypes & Component Spec

Normative for all admin surfaces under `templates/admin/**`. Companion to
`docs/SOT_RELATIONSHIP_MAP.md`: that document owns *who decides state*; this one
owns *how the UI projects it*. Where they touch, the relationship map wins.

Migration is **section by section**. This spec is the contract each section is
rebuilt against, so section two reuses section one instead of reinventing it.

**Status:** adopted 2026-07-20. Decisions log at the end.

---

## 1. Principles (non-negotiable)

These are not style preferences. They are the UI half of the source-of-truth
standard, and a redesigned section is not "done" until it satisfies them.

1. **Thin projection.** The UI renders server-owned contracts. It never decides
   business state, never computes derived state, never caches truth, and never
   branches business logic in a template or in Alpine. A control either *reads*
   a projection or *requests an action / reconcile* from the owning service's
   endpoint. Nothing in between.

2. **Color-as-meaning is server-owned.** Status color comes from the server's
   **semantic tone** contract (`positive | info | warning | negative | neutral`),
   rendered through `status_presentation_badge` / `status_badge` and the
   `.status-tone-*` renderers. The template must never re-derive tone from a
   string (`if status == "error" → red`), and never hardcode a status color.
   The server's resolver owns "what state this is and what tone represents it";
   the template only paints it. This is the same ownership rule as everything
   else in the fleet, applied to pixels.

3. **Color-as-brand is token-owned.** Brand color comes only from the
   design-system tokens (`--color-brand-*`, `--color-accent-*`, `--surface-*`,
   `--text-*`, `--border-*`). `/branding/theme.css` is authoritative at runtime
   and overrides every scale per tenant. No hardcoded hex in templates; no
   per-page literal accent (`color="amber"`, `color="indigo"`). The single
   interactive accent is the brand **accent (cyan)** scale. Per-section color
   is *not* wayfinding — the section nav is.

4. **Flat decoration.** Admin surfaces are calm and dense for all-day staff use.
   Do not use `ambient_background`, `.glass`, `.mesh-gradient`, `.text-gradient`,
   or `.noise-overlay` on admin surfaces. Depth comes from the plain
   `.shadow-premium` / border / surface tokens only. (Decoration is SOT-neutral;
   this is an aesthetic decision, recorded so it is applied uniformly.)

5. **Accessibility floor: WCAG 2.2 AA.** State is conveyed by icon **and** text,
   never color alone (`status_badge` already does this — keep it). Every
   interactive element has a visible focus state. Honor `prefers-reduced-motion`.
   Contrast holds in both light and dark themes.

6. **Density is config, not per-page.** Comfortable / compact is a user-level
   setting the shell applies; individual templates do not invent their own
   spacing scale.

---

## 2. The four archetypes

Every admin surface is one of four archetypes. The archetype dictates how
*context* is handled — which is the whole answer to "do we still need side
panels?": a persistent side panel is correct for exactly one archetype.

| Archetype | Use when | Context handling | Example sections |
|-----------|----------|------------------|------------------|
| **A · Triage workspace** | You process a live queue and always need the same context beside each item | **Persistent** three-pane: list · thread/work · context rail | `inbox`, `dispatch`, `alerts`, `service_requests` |
| **B · Record / 360** | You are looking at one entity | Context **promoted** to a `detail_header` hero + tabs. **No permanent rail.** | `customers` (subscriber 360), `resellers`, `vendors`, catalog item, `referrals` detail |
| **C · Operational canvas** | The data is spatial — topology, map, board | **Full-bleed** canvas + **on-demand inspector** that appears only on selection | `network`, `gis`, `provisioning` board |
| **D · Data / ledger** | You scan, filter, and act on many rows | **Full-width** `data_table`; detail opens in a **row drawer** or its own record page | `billing`, `reports`, catalog list, `integrations`, `system`, `drift`, `notifications`, `errors` |

**The rule:** persistent side rail → triage only. Everywhere else, context is a
header-hero (records) or a summonable inspector (canvases and tables). This is
what keeps the redesigned pages functional instead of squeezing every surface
into a cramped 336px column.

Reference mocks (design language, not production code): inbox (A), subscriber
360 (B), network topology (C). The data-ledger (D) reuses the existing
`data_table` macro plus the new row drawer.

---

## 3. Shared spine (build once, every section inherits)

- **Shell with slots** — `layouts/admin.html` provides: top bar, section nav,
  main content slot, and an **optional right slot** (the rail for archetype A,
  the inspector mount for C/D). Archetype B leaves the right slot empty.
- **Subscriber-context module** — one server contract, **three render modes**:
  `rail` (triage), `hero` (record), `inspector` (canvas/table). Same data, same
  fields, different container. Built once; this is the piece that makes the
  archetypes feel like one system.
- **Semantic tone system** — already shipped (`status-tone-*`). Reused as-is.
- **Command palette (⌘K)** — global jump-to and action runner. One component,
  mounted by the shell, available on every surface.

---

## 4. Component inventory

### Reuse as-is (already in `templates/components/ui/macros.html`)

`page_header`, `detail_header`, `tabs`, `stats_card`, `status_badge`,
`status_presentation_badge`, `card`, `data_table` + `table_head` + `table_row` +
`row_actions`, `info_row`, `search_input`, `filter_select`, `filter_bar`,
`empty_state`, `icon_badge`, `action_button`.

> Migration note: existing macros default several params to `color="amber"`.
> Redesigned callers pass no per-page color; the macros should default to the
> brand accent. Changing the *default* is a spec-level change (do it once, in the
> macro), not something each caller overrides.

### Net-new to build (proposed macro API — subject to review before the inbox build)

1. `triage_shell(list, thread, context)` — CSS-grid three-pane with responsive
   collapse (context drops < 1040px, list drops < 760px). Archetype A only.
2. `inspector(open, title, subtitle, tone="neutral")` + `caller()` body —
   summonable right panel with close control. Mounts in the shell right slot.
   Archetypes C and D.
3. `subscriber_context(subscriber, mode="rail|hero|inspector")` — the shared
   context module. `subscriber` is a server projection; the macro renders, never
   computes.
4. Message thread: `message(direction, ...)` (in/out bubble), `private_note(...)`,
   `system_event(...)` (auto-linked chip, e.g. "linked to outage"). Archetype A.
5. `command_palette()` — mounted by the shell.
6. `conversation_list_item(...)` — the triage list row.

Each net-new component lands in the macro library **and** gets a live example in
`templates/admin/design_system/` (the existing styleguide) so it is documented
where engineers already look.

---

## 5. SOT boundary — what the UI may and may not do

**May:** render a server projection; link to another surface; submit a form or
HTMX request to the **owning service's** endpoint to change source state or
request a reconcile; show optimistic pending state that the server confirms.

**Must not:** compute a derived field client-side; decide a status's tone, label,
or icon; hardcode a status color or brand hex; keep the only copy of any truth in
the DOM or Alpine state; branch business rules in a template. If a template needs
a value that does not exist on the projection, the fix is to add it to the
server-owned contract — not to derive it in Jinja.

---

## 6. Migration playbook (per section)

1. **Classify** the section's archetype (§2).
2. **Mock** if the layout is a real change (optional; skip for straight ports).
3. **Extract** any new shared piece into a macro + styleguide entry (§4) before
   using it, so the next section reuses it.
4. **Rebuild** the template against server contracts, consuming projections.
5. **Remove SOT leaks** in the touched files: re-derived tone, hardcoded status
   colors, literal per-page accents, raw hex, any client-side state decision.
6. **Add** the surface's components to the `design_system` styleguide.
7. **Ship** the section. New work stays on local seabone branches; no push / PR
   until Michael asks.

A section is done when: it renders in its archetype, carries zero hardcoded
status color or brand hex, passes AA in both themes, and its new components are
in the styleguide.

---

## 7. Section roadmap (`admin/**` → archetype, ordered by leverage)

Order is chosen so the earliest sections build the reusable spine the rest
inherit.

| # | Section | Archetype | Notes |
|---|---------|-----------|-------|
| 1 | `inbox` | A · Triage | **Flagship.** Today a list+filter (D) page; uplift converts it to the three-pane triage workspace. Builds the shell right-slot, subscriber-context module, message-thread + command palette — the whole spine. |
| 2 | `customers` (subscriber 360) | B · Record | Highest-traffic record; consumes the subscriber-context module in `hero` mode → cheap validation of the spine. |
| 3 | `network` | C · Canvas | Full-bleed topology; first use of the on-demand inspector. |
| 4 | `billing` | D · Ledger | First ledger + row drawer; also the highest-scrutiny SOT surface (money). |
| 5 | `dispatch`, `service_requests`, `alerts` | A · Triage | Reuse the inbox shell. |
| 6 | `resellers`, `vendors` | B · Record | Reuse the 360. |
| 7 | `gis`, `provisioning` | C · Canvas | Reuse the inspector. |
| 8 | `reports`, `catalog`, `integrations`, `referrals`, `system`, `drift`, `notifications`, `errors` | D · Ledger | Reuse the ledger + drawer. |
| — | `dashboard` | (overview) | Composed of `stats_card` + small archetype fragments; do last, once the vocabulary is settled. |

---

## 8. Decisions log

- **2026-07-20** — Consolidation: CRM is absorbed into sub; all operational
  surfaces get one design language (Michael).
- **2026-07-20** — Decoration: **flat everywhere** on admin surfaces; retire
  glass / mesh-gradient / noise / ambient washes (Michael). Aesthetic, not SOT.
- **2026-07-20** — Color-as-meaning: **server-owned semantic tone** only; remove
  re-derived tone and hardcoded status colors as sections are touched. SOT.
- **2026-07-20** — Interactive accent: single **brand accent (cyan)**; retire
  per-page literal accents. Brand color via tokens / `/branding/theme.css` only.
- **2026-07-20** — Build order: spec first, then `inbox` as the first real
  section (Michael).
