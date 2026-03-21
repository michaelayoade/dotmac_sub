# Production UI Brief

This app is an operations console, not a marketing site. The UI should optimize for scan speed, task completion, and safe decision-making under load.

It is specifically an ISP operations console. Pages must support subscriber support, provisioning, NOC monitoring, collections, and field operations without forcing operators to translate between billing and network contexts by memory alone.

## Core Principles

1. Density before decoration.
Use strong spacing and hierarchy, but keep key records, statuses, and actions visible without excessive scrolling.

2. One primary action per screen.
Each page should have a clear primary CTA. Secondary actions belong in cards, row actions, or overflow menus.

3. Color carries meaning, not novelty.
Use module accents for orientation only. Reserve semantic colors for state:
- `emerald`: success, paid, online
- `amber`: warning, pending, degraded
- `rose`: overdue, failed, destructive
- `slate`: neutral, archival, disabled

4. Tables are the product.
List pages should prioritize fast filtering, sticky context, readable row density, and bulk actions over oversized hero sections.

5. Dark mode must remain readable.
Avoid low-contrast slate-on-slate combinations for labels, helper text, and dividers. Dense admin views need stronger separation than showcase pages.

6. Network and billing context must stay connected.
Operators should be able to move from subscriber identity to ONT/OLT, PPPoE/RADIUS, invoices, tickets, and service state without losing context.

## ISP Operational Realities

This product serves multiple operator modes. The UI should acknowledge them explicitly:

- Support / care: fast subscriber lookup, service state, outstanding balance, last online time, open tickets, and recent changes.
- NOC: alarms, degraded service, outage grouping, device health, ONT signal quality, and current exceptions.
- Provisioning / activation: service-order progress, ONT/OLT assignment, VLAN/profile configuration, and dry-run / execute confidence.
- Field operations: precise location, device identifiers, install status, appointment readiness, and latest notes.
- Finance / collections: overdue state, risk, promise-to-pay status, suspension state, and service impact.

The same account can appear in all of these workflows. The design system must preserve a stable identity model across them.

## Hard Rules

These are implementation rules, not suggestions.

1. One primary CTA in the context bar.
- Every page gets at most one primary button in the page header.
- Additional actions must be secondary, ghost, row-level, or inside the work surface.

2. Decision layers stay compact.
- Dashboards: 4-6 KPI tiles max in the first decision layer.
- Detail pages: 1 summary card plus up to 4 supporting metrics.
- List pages: if a KPI strip exists, cap it at 4 items.

3. Orientation color and semantic color are different systems.
- Module accent colors are for headers, icon badges, selected tabs, and section orientation.
- Semantic colors are for status and risk:
  - `emerald`: success, healthy, online, paid
  - `amber`: warning, pending, degraded
  - `rose`: failed, critical, overdue, destructive
  - `slate`: neutral, archived, disabled
- Never use a module accent alone to communicate state.

4. First viewport must support a real task.
- A user should see a usable decision layer and at least one actionable work surface without excessive scrolling.
- On list pages, the table or first rows must be visible on common laptop heights.

5. Decorative treatments are subordinate to data.
- Large hero treatments are allowed only when they improve orientation and do not push the work surface too far below the fold.
- Decorative glow, oversized gradients, and motion must not compete with primary data.

6. Partial refreshes must preserve layout stability.
- HTMX-polled widgets must keep stable height between loading and loaded states.
- Polling failures should degrade to inline warnings, not blank regions.
- Auto-refresh must not move controls, pagination, or surrounding cards.

7. Dense admin pages must use strong readable contrast.
- Helper text must remain readable in dark mode.
- Borders and surface separation must be visible at a glance.
- Do not stack muted slate tones without a stronger dividing line, surface change, or semantic signal.

8. Subscriber identity must be cross-domain.
- Subscriber pages must treat customer name alone as insufficient identity.
- Show operational identifiers that let staff correlate records across systems: account number, subscriber code, ONT serial, PPPoE username, IP, MAC, OLT/PON location, or phone number as relevant.
- Technical identifiers should be copyable and visually distinct from descriptive labels.

9. Outages and degradation are first-class states.
- Distinguish `degraded` from `offline` and `critical`.
- Group outage views by operational blast radius where possible: site, OLT, PON port, zone, POP, cabinet, or upstream dependency.
- Surface customer impact counts near outage state, not buried in lower cards or detail tabs.

10. Search must reflect ISP lookup behavior.
- Subscriber and network search must support names plus operational identifiers such as phone, account number, invoice number, ONT serial, PPPoE username, IP, MAC, and OLT/port references.
- Search placeholders and empty states should teach operators what identifiers are supported.

## Page Anatomy

Every admin page should follow this structure:

1. Context bar
- Breadcrumb
- Page title
- Short subtitle with operational meaning
- Primary action

2. Decision layer
- KPI strip, status summary, or alert banner
- Keep to 4-6 items max

3. Control layer
- Search
- Filters
- Saved/suggested scopes where useful
- Bulk actions only when selection exists

4. Work surface
- Table, chart grid, form, or detail layout
- Empty states and loading states must preserve layout stability

5. Live/secondary surface
- Optional HTMX-polled widgets, audit feeds, system health, or lower-priority summaries
- Must not displace the primary work surface

## ISP Page Types

### Subscriber Support Surfaces
- Lead with identity, service state, debt/risk, and last connectivity signal.
- Show the shortest path to suspend, restore, contact, troubleshoot, or navigate to network detail.
- Keep billing and technical state visible together in the first screen.

### NOC / Monitoring Surfaces
- Lead with exceptions, grouped impact, and acknowledgement / resolution actions.
- Prefer grouped outage views over raw alert floods.
- Trend charts are supporting evidence; current impact and probable blast radius come first.

### Provisioning / Activation Surfaces
- Use explicit step progress, prerequisite checks, and preview-before-execute patterns.
- Show OLT, ONT, VLAN, profile, and subscriber assignment context together.
- Async jobs must expose current step, latest result, and safe retry guidance.

### Map / Topology Surfaces
- Maps are operational tools, not decoration.
- Always pair map markers with a synchronized list, table, or detail rail.
- Marker color alone must not convey alarm severity or install state.
- Clustered views should surface counts, severity mix, and drill-in paths.

## Recommended Patterns

### Dashboard
- Keep top KPIs compact.
- Show only exception-based alerts in the attention bar.
- Quick links should be grouped by workflow, not by every module in the system.
- Charts should answer one question each; do not stack many equally weighted charts on first view.

Required dashboard recipe:
- Context bar with one primary action.
- Exception-only attention banner.
- Compact KPI strip with 4-6 tiles.
- Up to 3 workflow launchpad groups.
- One main work surface block plus one secondary operational block.
- Optional live partials below the first work surface.

Discouraged dashboard patterns:
- Full-width marketing-style hero sections.
- More than 2 equally weighted chart cards above the first work surface.
- Module directories masquerading as quick links.
- KPI walls made from oversized showcase cards.

### List Pages
- Default to tables with sticky headers.
- Search and filters stay above the table, not mixed into hero cards.
- Row actions should be consistent across modules.
- Bulk actions should use non-blocking confirmations where possible.
- ISP list pages should allow lookup by operational identifiers, not just human-readable names.

Required list-page rules:
- Control layer belongs directly above the table.
- Table header context must remain visible while scanning dense lists.
- Empty states should preserve the table/card footprint.
- Bulk actions appear only when selection exists.
- Where applicable, show both descriptive identity and technical identity in each row.

### Detail Pages
- Summary card first: identity, status, owner, timestamps.
- Actions grouped on the right or in a fixed action rail.
- Related activity, billing, and audit history should be separate sections.

Required detail-page rules:
- The first card must establish identity, state, and ownership.
- Follow-up sections should be separated by task type, not by ORM model.
- Destructive actions must be visually separated from routine edits.
- For ISP entities, the first card should expose the identifiers needed for cross-system correlation.
- If the entity can affect service delivery, show current operational state before long descriptive metadata.

### Charts
- Prefer line/bar charts over doughnuts unless the composition question is the main task.
- Show totals and comparison deltas outside the chart so the graphic is not the only source of truth.
- Never rely on color alone to distinguish critical series.

Required chart rules:
- No more than 2 charts above the first major work surface.
- Every chart must answer one operational question.
- Show key totals, deltas, or thresholds outside the chart.
- If a doughnut is used, the composition question must be the primary reason the chart exists.
- For network charts, annotate thresholds such as low signal, capacity, or error boundaries instead of showing trend lines without operating context.

### HTMX / Partial States
- Polling widgets must reserve stable height.
- Partial loading states should use skeletons or placeholders with fixed dimensions.
- Failed partial updates should render inline warning content inside the same container.
- Polled timestamps or badges must update without reflowing nearby actions.
- Refreshing network status should not detach acknowledgement state, selection state, or open troubleshooting context.

### Responsive Behavior
- Dashboard KPI strips collapse to 2 columns before cards become oversized.
- Launchpad groups should remain list-like on mobile; do not compress them into tiny icon grids.
- List-page filters stack vertically before row density is reduced.
- Detail-page action rails move below the summary card on narrower screens.
- On mobile support screens, critical identity and connectivity state must remain visible before secondary notes or charts.

## Visual Direction

- Typography: strong display face for page titles only; use the sans face for dense data views.
- Surface model: dark shell, brighter content surfaces, clear borders, restrained glow.
- Motion: subtle load-in and hover elevation only; no decorative animation on critical workflows.
- Icons: support recognition, never replace labels in dense views.
- Maps, topology lines, and signal graphics should feel utilitarian and legible rather than decorative.

## Component Bias

Preferred:
- Compact KPI tiles
- Clear bordered cards
- Dense tables with readable headers
- Inline status pills with icon support
- Workflow-grouped launchpads
- Split identity blocks that pair customer-facing names with technical identifiers
- Outage summaries grouped by blast radius and impact count

Discouraged:
- Oversized hero metric cards
- Decorative chart walls
- Mixed semantic and module colors in one state treatment
- Empty states that collapse layout height
- Floating action bars that duplicate the page header CTA
- Alarm feeds that bury impact counts and affected scope
- Device or subscriber pages that hide ONT/OLT/PPPoE identifiers in secondary tabs

## Review Checklist

Use this in design reviews and before merge:

1. Does the page have exactly one primary action in the context bar?
2. Is the first viewport usable for a real task?
3. Are semantic colors used for status instead of module accents?
4. Is helper text readable in dark mode without strain?
5. Are KPI strips capped appropriately for the page type?
6. Are quick links grouped by workflow instead of by module inventory?
7. Are charts limited, purposeful, and supported by totals outside the chart?
8. Do empty/loading/polled states preserve layout stability?
9. On list pages, are search and filters directly above the table?
10. On mobile, does the page keep decision-making readable instead of merely “fitting”?
11. Can an operator correlate the record across billing, network, and provisioning without extra navigation?
12. Are outages/degraded states grouped by impact rather than shown only as individual events?

## Immediate Cleanup Targets

1. Standardize breadcrumbs and page context across all admin pages.
2. Reduce oversized dashboard modules into denser operational tiles.
3. Normalize filters, table headers, and empty states across subscribers, billing, and network pages.
4. Replace alert/confirm browser dialogs with shared inline or modal patterns.
5. Consolidate chart bootstrapping into reusable helpers instead of page-local script blocks.
6. Standardize subscriber identity blocks so account, billing, and network identifiers appear consistently.
7. Normalize outage, signal, and degraded-state semantics across monitoring, ONT, and subscriber views.
