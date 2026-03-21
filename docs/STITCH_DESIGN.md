# DESIGN.md — DotMac Sub ISP Admin Portal

Paste this into Google Stitch as your project DESIGN.md context.
Then prompt for specific pages using the examples at the bottom.

---

## Product

DotMac Sub is an admin portal for ISP operations — managing subscribers,
billing, network equipment (OLTs, ONTs, CPEs), service provisioning,
and reporting. Target users are ISP NOC engineers, billing staff, and admins.
The UI is data-dense, desktop-first, and designed for fast daily operations.

## Tech Stack

- **CSS Framework:** Tailwind CSS v4
- **Interactivity:** Alpine.js (client-side state) + HTMX (server-driven updates)
- **Icons:** Inline SVG (no icon library)
- **Fonts:** Outfit (headings), Plus Jakarta Sans (body)
- **Dark Mode:** Class-based toggle (always provide both light + dark variants)

## Typography

| Role | Classes |
|------|---------|
| Page title (h1) | `font-display text-2xl font-bold tracking-tight text-slate-900 dark:text-white` |
| Section title (h2) | `font-display text-xl font-bold tracking-tight text-slate-900 dark:text-white` |
| Card title (h3) | `text-lg font-bold text-slate-900 dark:text-white` |
| Body | `text-base text-slate-900 dark:text-white` |
| Dense data / tables | `text-sm text-slate-900 dark:text-white` |
| Labels | `text-sm font-medium text-slate-700 dark:text-slate-300` |
| Helper / muted | `text-xs text-slate-500 dark:text-slate-400` |
| Section eyebrow | `text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400` |
| Financial values | `tabular-nums font-mono` |
| IP / MAC addresses | `font-mono text-sm` |

## Color System

### Module Accent Colors (gradient pairs)

Each module has a primary + secondary color used in headers, buttons, and stat cards.

| Module | Primary | Secondary | Use For |
|--------|---------|-----------|---------|
| Subscribers / Customers | amber | orange | Page headers, action buttons, filter bars |
| Billing / Invoices | emerald | teal | Invoice detail, payment status, financial KPIs |
| Catalog / Offers | violet | purple | Plan cards, pricing, subscription stats |
| Network / Infrastructure | blue | indigo | Device status, topology, IP management |
| Speed Tests | cyan | sky | Speedtest results, bandwidth metrics |
| Provisioning | amber | orange | Service orders, workflow steps |
| System / Admin | slate | gray | Settings, users, roles, server health |
| VPN / WireGuard | cyan | teal | VPN status, peer management |
| Reports / Analytics | teal | emerald | Charts, KPI cards |
| GIS / Mapping | green | emerald | Map markers, coverage areas |
| Notifications | rose | pink | Alert badges, on-call |
| Integrations | indigo | blue | Connectors, webhooks |

### Status Colors

| Status | Color | Light Classes | Dark Classes |
|--------|-------|--------------|-------------|
| Active / Success / Paid | emerald | `bg-emerald-100 text-emerald-800` | `dark:bg-emerald-900 dark:text-emerald-200` |
| Warning / Suspended | amber | `bg-amber-100 text-amber-800` | `dark:bg-amber-900 dark:text-amber-200` |
| Error / Failed / Canceled / Overdue | rose | `bg-rose-100 text-rose-800` | `dark:bg-rose-900 dark:text-rose-200` |
| Info / Pending | blue | `bg-blue-100 text-blue-800` | `dark:bg-blue-900 dark:text-blue-200` |
| Neutral / Draft | slate | `bg-slate-100 text-slate-800` | `dark:bg-slate-700 dark:text-slate-200` |

### Signal Strength (ISP-specific)

| Quality | Color | Threshold |
|---------|-------|-----------|
| Good | emerald | > -25 dBm |
| Acceptable | amber | -25 to -28 dBm |
| Poor | rose | < -28 dBm |

## Components

### Page Header

Gradient background with module accent colors, title, subtitle, and action buttons.

```html
<div class="relative overflow-hidden rounded-2xl border border-slate-200/60 bg-gradient-to-br from-{color}-50 to-{color2}-50/50 p-6 shadow-sm dark:border-slate-700/60 dark:from-{color}-900/20 dark:to-{color2}-900/10">
    <h1 class="font-display text-2xl font-bold tracking-tight text-slate-900 dark:text-white">Page Title</h1>
    <p class="mt-1 text-sm text-slate-600 dark:text-slate-300">Page description</p>
    <!-- Action buttons go top-right -->
</div>
```

### Cards

```html
<div class="rounded-2xl border border-slate-200/60 bg-white p-5 shadow-sm dark:border-slate-700/60 dark:bg-slate-800">
    <!-- Card content -->
</div>
```

### KPI / Stat Cards

Compact cards showing a metric value with label and optional trend indicator:

```html
<div class="group rounded-2xl border border-slate-200/70 bg-white p-3.5 shadow-sm transition-all hover:-translate-y-0.5 hover:shadow-md dark:border-slate-700/70 dark:bg-slate-800">
    <p class="text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-500 dark:text-slate-400">Label</p>
    <p class="mt-2.5 text-[clamp(1.25rem,2.2vw,1.8rem)] font-bold leading-none tabular-nums text-slate-900 dark:text-white">1,234</p>
    <p class="mt-1.5 text-sm text-slate-600 dark:text-slate-300">Helper text</p>
</div>
```

### Buttons

- **Primary:** `rounded-xl bg-gradient-to-r from-{color}-500 to-{color2}-600 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-{color}-500/25 hover:-translate-y-0.5 hover:shadow-{color}-500/30`
- **Secondary:** `rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-700 hover:border-{color}-300 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300`
- **Ghost:** `rounded-xl px-4 py-2 text-sm font-semibold text-{color}-600 hover:bg-{color}-50 dark:text-{color}-400 dark:hover:bg-{color}-900/20`
- **Danger:** `rounded-xl bg-gradient-to-r from-red-500 to-rose-600 px-4 py-2 text-sm font-semibold text-white`

### Status Badges

Pill-shaped with icon dot for accessibility:

```html
<span class="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200">
    <span class="h-1.5 w-1.5 rounded-full bg-emerald-500"></span>
    Active
</span>
```

### Tables

```html
<table class="w-full">
    <thead>
        <tr class="border-b border-slate-200 dark:border-slate-700">
            <th class="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-500 dark:text-slate-400">Column</th>
        </tr>
    </thead>
    <tbody class="divide-y divide-slate-200 dark:divide-slate-700">
        <tr class="hover:bg-slate-50 dark:hover:bg-slate-700/50">
            <td class="px-4 py-3 text-sm text-slate-900 dark:text-white">Value</td>
        </tr>
    </tbody>
</table>
```

### Filter Bar

Horizontal bar above tables with search, dropdowns, and action buttons:

```html
<div class="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200/60 bg-white p-3 shadow-sm dark:border-slate-700/60 dark:bg-slate-800">
    <div class="relative flex-1">
        <svg class="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400"><!-- search icon --></svg>
        <input type="search" placeholder="Search..." class="w-full rounded-lg border border-slate-300 bg-white py-2 pl-9 pr-3 text-sm dark:border-slate-600 dark:bg-slate-700 dark:text-white">
    </div>
    <select class="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-700 dark:text-white">
        <option>All statuses</option>
    </select>
</div>
```

### Empty State

```html
<div class="rounded-2xl border border-dashed border-slate-300 bg-slate-50/50 p-12 text-center dark:border-slate-600 dark:bg-slate-800/50">
    <div class="mx-auto h-16 w-16 rounded-2xl bg-gradient-to-br from-{color}-100 to-{color2}-100 p-4 dark:from-{color}-900/30 dark:to-{color2}-900/30">
        <!-- SVG icon -->
    </div>
    <h3 class="mt-4 text-sm font-semibold text-slate-900 dark:text-white">No items found</h3>
    <p class="mt-1.5 text-sm text-slate-500 dark:text-slate-400">Get started by creating your first item.</p>
    <button class="mt-4 rounded-xl bg-gradient-to-r from-{color}-500 to-{color2}-600 px-4 py-2 text-sm font-semibold text-white">Create Item</button>
</div>
```

### Ambient Background

Subtle gradient orbs behind page content (purely decorative):

```html
<div class="pointer-events-none fixed inset-0 overflow-hidden">
    <div class="absolute -top-40 right-0 h-[500px] w-[500px] rounded-full bg-gradient-to-br from-{color}-400/5 to-{color2}-500/5 blur-3xl"></div>
    <div class="absolute -bottom-40 -left-40 h-[400px] w-[400px] rounded-full bg-gradient-to-tr from-{color2}-400/5 to-{color}-500/5 blur-3xl"></div>
</div>
```

## Layout

### Overall Structure

- **Sidebar:** 240px fixed, collapsible to 80px icon-only mode. White bg, border-right.
- **Main content:** `flex-1` with `p-6` padding, `bg-slate-50 dark:bg-slate-900`
- **Content max-width:** None (fills available space)

### Page Types

**Dashboard:**
- KPI stat cards in CSS grid: `grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4`
- Attention banner (if items need action)
- Quick-launch cards grouped by domain
- Recent activity feed

**List Page:**
- Page header with accent gradient
- Filter bar (search + dropdowns + action button)
- Data table with sortable columns
- Pagination footer (total count, page range, prev/next)

**Detail Page:**
- Page header with entity name + status badge
- Tab navigation for sections
- Content cards within each tab
- Action buttons in header (edit, delete, etc.)

**Form Page:**
- Page header with breadcrumb
- Card-wrapped form sections
- Grouped fields (identification, relationships, config, financial, notes)
- Submit/cancel buttons at bottom

## Spacing

- Section padding: `p-6`
- Card padding: `p-5`
- Gap between cards: `gap-4` or `gap-6`
- Stack spacing: `space-y-4` or `space-y-6`
- Table cell padding: `px-4 py-3`

## Dark Mode

Always provide both variants. Common pairs:

| Element | Light | Dark |
|---------|-------|------|
| Page background | `bg-slate-50` | `dark:bg-slate-900` |
| Card background | `bg-white` | `dark:bg-slate-800` |
| Text primary | `text-slate-900` | `dark:text-white` |
| Text secondary | `text-slate-600` | `dark:text-slate-300` |
| Text muted | `text-slate-500` | `dark:text-slate-400` |
| Border | `border-slate-200` | `dark:border-slate-700` |
| Border subtle | `border-slate-200/60` | `dark:border-slate-700/60` |
| Table row hover | `hover:bg-slate-50` | `dark:hover:bg-slate-700/50` |
| Alternating rows | `bg-slate-50` | `dark:bg-slate-900` |

## Data Display (ISP Domain)

| Data Type | Format | Example | Style |
|-----------|--------|---------|-------|
| Currency | Symbol + 2 decimals | `NGN 5,000.00` | `tabular-nums font-mono` |
| Bandwidth | Value + unit | `100 Mbps`, `1 Gbps` | regular |
| IP address | Dotted quad | `192.168.1.1` | `font-mono text-sm` |
| MAC address | Colon-separated | `AA:BB:CC:DD:EE:FF` | `font-mono text-sm` |
| Signal strength | dBm | `-24.5 dBm` | colored by threshold |
| Uptime | Human readable | `3d 14h 22m` | regular |
| Date (recent) | Relative | `2 hours ago` | regular |
| Date (old) | ISO | `2024-01-15` | regular |
| Phone | E.164 | `+234 801 234 5678` | regular |
| Percentage | 1 decimal | `95.5%` | `tabular-nums` |

## Interactions

- Transitions: max 300ms, `ease-out`
- Hover lift on cards: `hover:-translate-y-0.5`
- Skeleton loading placeholders during data fetch
- Toast notifications: bottom-right, auto-dismiss 4s
- Modals: backdrop blur, centered, `max-w-lg`
- Live search: debounced 300ms

## Accessibility

- WCAG 2.2 AA
- All inputs have `<label>` elements
- Status badges use icon dots (not color-only)
- Focus rings visible in both light and dark mode
- Keyboard navigation for all interactive elements

---

## Example Prompts for Stitch

Copy-paste these into Stitch after setting DESIGN.md as context:

### Dashboard
> ISP admin dashboard with: 8 KPI stat cards in a 4-column grid (subscribers, active, online sessions, network devices, ONTs, revenue this month, overdue receivables, total alarms), an attention banner showing items needing action, and 4 quick-launch card groups (Subscribers, Network, Billing, System) each with 4-5 link items. Use amber/slate accent. Include ambient background orbs.

### Subscriber List
> Subscriber list page for an ISP admin portal. Page header with amber/orange gradient accent. Filter bar with search input, status dropdown (all/active/suspended/canceled), type dropdown (person/organization), and "Add Subscriber" primary button. Data table with columns: account number (mono), name, status (badge), plan name, monthly amount (mono, right-aligned), location, created date. Pagination at bottom. Use the subscriber table pattern.

### OLT Device Detail
> OLT network device detail page. Blue/indigo accent. Header shows device name, IP address in mono, and status badge (online/offline). Tabs: Overview, ONTs, PON Ports, Alarms, Config. Overview tab has 6 stat cards (total ONTs, online ONTs, offline ONTs, low signal, uptime, firmware version) and a device info card with model, serial, location, last polled. ONTs tab is a filtered table.

### Invoice Detail
> Invoice detail page for ISP billing. Emerald/teal accent. Header with invoice number, subscriber name, status badge (paid/overdue/draft), and amount in large tabular-nums font. Two-column layout: left side has line items table (description, quantity, unit price, total), right side has summary card (subtotal, tax, total, amount paid, balance due). Action buttons: Send, Record Payment, Download PDF.

### Network Monitoring Dashboard
> Real-time network monitoring dashboard. Blue/indigo accent. Top row: 4 KPI cards (devices online, devices offline, active alarms, average uptime). Middle: two side-by-side cards — left is alarm severity breakdown (critical/major/minor/warning with colored counts), right is device type distribution. Bottom: recent alarms table with columns (time, device, severity badge, description, status). Include ambient background.
