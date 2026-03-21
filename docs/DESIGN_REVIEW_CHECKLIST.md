# Design Review Checklist

Use this checklist for UI-heavy changes before merge. It is derived from:
- `docs/PRODUCTION_UI_BRIEF.md`
- `docs/FRONTEND_SPEC.md`

This is not a generic polish list. It is a release gate for dense admin UI.

## How To Use

Apply the checklist to:
- new admin pages
- substantial redesigns
- changes to shared UI macros
- dashboard, table, filter, chart, and detail-page changes

If a section does not apply, mark it `N/A` explicitly.

## Context Bar

- [ ] Breadcrumbs correctly reflect the current page and parent section.
- [ ] The page title is specific and operationally meaningful.
- [ ] The subtitle explains the task or decision the page supports.
- [ ] There is exactly one primary action in the page header.
- [ ] Any additional header actions are secondary or ghost actions.

## Decision Layer

- [ ] The first decision layer is compact and scannable.
- [ ] Dashboard KPI strips contain 4-6 items max.
- [ ] List-page KPI strips, if present, contain 4 items max.
- [ ] Semantic state is not conveyed by accent color alone.
- [ ] Exception banners only show actionable warnings or failures.

## Control Layer

- [ ] Search and filters sit directly above the primary work surface.
- [ ] Search and filters are not embedded in oversized hero cards.
- [ ] Filter labels and helper text are readable in dark mode.
- [ ] Bulk actions only appear when selection exists.
- [ ] Controls remain stable during HTMX refreshes.

## Work Surface

- [ ] The first viewport supports a real task without excessive scrolling.
- [ ] List pages show table context or first rows on common laptop heights.
- [ ] Empty states preserve the same footprint as the loaded surface.
- [ ] Loading or polling states do not collapse layout height.
- [ ] Row actions are consistent with neighboring modules.
- [ ] Subscriber-facing surfaces expose both human identity and service identity where needed.
- [ ] NOC-facing surfaces show impact counts or blast radius before deep drill-down.

## Color And State

- [ ] Module accents are used for orientation, not semantic state.
- [ ] `emerald` means success/healthy/online/paid.
- [ ] `amber` means warning/pending/degraded.
- [ ] `rose` means failed/critical/destructive/overdue.
- [ ] `slate` means neutral/archived/disabled.
- [ ] Critical series or states are not distinguishable by color alone.

## Typography And Density

- [ ] Page titles use the display face only where intended.
- [ ] Dense data views use the sans/body face.
- [ ] Numeric values that benefit from alignment use `tabular-nums`.
- [ ] Financial and technical identifiers use `font-mono` where appropriate.
- [ ] Helper text remains readable and is not too faint in dark mode.
- [ ] Cards, tables, and sections use borders/surfaces clearly enough for fast scanning.

## Dashboard-Specific

- [ ] The dashboard has one primary action only.
- [ ] Top KPI tiles are compact rather than showcase-style.
- [ ] Quick links are grouped by workflow, not by module inventory.
- [ ] No more than 2 equally weighted charts appear above the first work surface.
- [ ] The first work surface is operational content, not decorative summary.
- [ ] Lower live widgets like server health do not displace the main work surface.
- [ ] If outage or collections risk is a top concern, impact is visible without drilling into a secondary page.

## Chart-Specific

- [ ] Each chart answers one operational question.
- [ ] Totals, thresholds, or deltas are shown outside the chart.
- [ ] Line/bar charts are preferred over doughnuts unless composition is the question.
- [ ] Doughnut charts are not used as default decoration.
- [ ] Chart containers have stable heights and do not crop content.

## HTMX / Partial Refresh

- [ ] HTMX-polled widgets reserve stable height.
- [ ] A failed partial update degrades to inline warning content in the same region.
- [ ] Polling does not move nearby controls, pagination, or actions.
- [ ] Auto-refreshed timestamps/badges update without visual reflow.

## Responsive Behavior

- [ ] The page remains usable on mobile, not merely compressed.
- [ ] Dashboard KPI strips collapse to 2-column layouts before tiles become oversized.
- [ ] Launchpad groups stay readable as lists on smaller screens.
- [ ] List-page filters stack before row density becomes unreadable.
- [ ] Detail-page action rails move below the summary card on narrower screens.
- [ ] Mobile subscriber/support screens keep status plus key service identifiers visible before long notes or charts.

## ISP-Specific Checks

- [ ] Search fields teach and support operational identifiers such as phone, account number, ONT serial, PPPoE username, IP, or MAC.
- [ ] Subscriber service state, payment state, and device state are distinct when all three matter.
- [ ] Outage/degraded views group events by impact area where possible.
- [ ] Technical identifiers are easy to copy and visually distinguishable from labels.
- [ ] Maps or topology views include a synchronized list, table, or side rail.

## Shared Macros / System Changes

- [ ] Changes to shared macros do not silently drift away from the production brief.
- [ ] New macros reinforce the preferred patterns in the design guide.
- [ ] Shared components do not encourage oversized decorative layouts by default.
- [ ] New patterns are documented in `docs/FRONTEND_SPEC.md` when they become standard.

## Review Summary

Record the outcome in the PR:

- Scope reviewed:
- Screens / templates touched:
- Checklist exceptions:
- Follow-up issues:
