---
version: "1.0"
name: "DotMac Sub"
description: "Multi-tenant subscription management system for ISPs and fiber network operators. Designed for NOC technicians who need density, speed, and accuracy under pressure."

colors:
  # Primary - Teal/Cyan (network, primary actions)
  primary-50: "#ecfeff"
  primary-100: "#cffafe"
  primary-200: "#a5f3fc"
  primary-300: "#67e8f9"
  primary-400: "#22d3ee"
  primary-500: "#06b6d4"
  primary-600: "#0891b2"
  primary-700: "#0e7490"
  primary-800: "#155e75"
  primary-900: "#164e63"
  primary-950: "#083344"

  # Accent - Warm Orange (contrast, attention)
  accent-50: "#fff7ed"
  accent-100: "#ffedd5"
  accent-200: "#fed7aa"
  accent-300: "#fdba74"
  accent-400: "#fb923c"
  accent-500: "#f97316"
  accent-600: "#ea580c"
  accent-700: "#c2410c"
  accent-800: "#9a3412"
  accent-900: "#7c2d12"
  accent-950: "#431407"

  # Semantic - Status colors (contractual meaning)
  success: "#10b981"       # emerald-500 - healthy, online, complete
  warning: "#f59e0b"       # amber-500 - needs attention, degraded
  error: "#f43f5e"         # rose-500 - critical, offline, failed
  info: "#3b82f6"          # blue-500 - network, informational

  # Semantic - Domain colors
  network: "#3b82f6"       # blue-500 - network devices, IPs
  identity: "#8b5cf6"      # violet-500 - people, accounts, users
  billing: "#ec4899"       # pink-500 - invoices, payments
  infrastructure: "#a855f7" # purple-500 - OLTs, fiber plant

  # Neutrals - Slate scale
  neutral-50: "#f8fafc"
  neutral-100: "#f1f5f9"
  neutral-200: "#e2e8f0"
  neutral-300: "#cbd5e1"
  neutral-400: "#94a3b8"
  neutral-500: "#64748b"
  neutral-600: "#475569"
  neutral-700: "#334155"
  neutral-800: "#1e293b"
  neutral-900: "#0f172a"
  neutral-950: "#020617"

  # Surfaces
  surface-light: "#ffffff"
  surface-dark: "#0f172a"
  background-light: "#f1f5f9"
  background-dark: "#020617"

typography:
  display-xl:
    fontFamily: "Outfit, system-ui, sans-serif"
    fontSize: "36px"
    fontWeight: 700
    lineHeight: 1.1
    letterSpacing: "-0.025em"
  display-lg:
    fontFamily: "Outfit, system-ui, sans-serif"
    fontSize: "30px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "-0.025em"
  heading-lg:
    fontFamily: "Outfit, system-ui, sans-serif"
    fontSize: "24px"
    fontWeight: 700
    lineHeight: 1.3
    letterSpacing: "-0.02em"
  heading-md:
    fontFamily: "Outfit, system-ui, sans-serif"
    fontSize: "20px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "-0.01em"
  heading-sm:
    fontFamily: "Outfit, system-ui, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.5
  body-lg:
    fontFamily: "Plus Jakarta Sans, system-ui, sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.6
  body-md:
    fontFamily: "Plus Jakarta Sans, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  body-sm:
    fontFamily: "Plus Jakarta Sans, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Plus Jakarta Sans, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1.4
  caption:
    fontFamily: "Plus Jakarta Sans, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 600
    lineHeight: 1.3
    letterSpacing: "0.05em"
    textTransform: "uppercase"
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "tnum"

rounded:
  none: "0"
  sm: "4px"
  md: "8px"
  lg: "12px"
  xl: "16px"
  2xl: "20px"
  3xl: "24px"
  full: "9999px"

spacing:
  0: "0"
  1: "4px"
  2: "8px"
  2.5: "10px"
  3: "12px"
  4: "16px"
  5: "20px"
  6: "24px"
  8: "32px"
  10: "40px"
  12: "48px"
  16: "64px"

components:
  button-primary:
    backgroundColor: "linear-gradient(to right, {colors.primary-500}, {colors.primary-600})"
    textColor: "#ffffff"
    typography: "{typography.body-md}"
    fontWeight: 600
    rounded: "{rounded.xl}"
    padding: "10px 16px"
    shadow: "0 10px 15px -3px rgb(6 182 212 / 0.25)"
  button-primary-hover:
    shadow: "0 20px 25px -5px rgb(6 182 212 / 0.30)"
    transform: "translateY(-2px)"
  button-secondary:
    backgroundColor: "{colors.surface-light}"
    textColor: "{colors.neutral-700}"
    borderColor: "{colors.neutral-200}"
    typography: "{typography.body-md}"
    fontWeight: 600
    rounded: "{rounded.xl}"
    padding: "10px 16px"
  button-secondary-hover:
    borderColor: "{colors.primary-300}"
    backgroundColor: "{colors.primary-50}"
    textColor: "{colors.primary-700}"
  button-danger:
    backgroundColor: "linear-gradient(to right, #ef4444, {colors.error})"
    textColor: "#ffffff"
    typography: "{typography.body-md}"
    fontWeight: 600
    rounded: "{rounded.xl}"
    padding: "10px 16px"
    shadow: "0 10px 15px -3px rgb(239 68 68 / 0.25)"
  card:
    backgroundColor: "{colors.surface-light}"
    borderColor: "{colors.neutral-200}"
    borderWidth: "1px"
    rounded: "{rounded.2xl}"
    padding: "{spacing.6}"
    shadow: "0 1px 3px 0 rgb(0 0 0 / 0.1)"
  card-dark:
    backgroundColor: "{colors.neutral-800}"
    borderColor: "{colors.neutral-700}"
  input:
    backgroundColor: "{colors.neutral-50}"
    textColor: "{colors.neutral-700}"
    borderColor: "{colors.neutral-300}"
    borderWidth: "1px"
    rounded: "{rounded.lg}"
    padding: "8px 12px"
    typography: "{typography.body-md}"
  input-focus:
    borderColor: "{colors.primary-500}"
    ringColor: "{colors.primary-500}"
    ringWidth: "1px"
  badge-success:
    backgroundColor: "{colors.success}"
    textColor: "#ffffff"
    rounded: "{rounded.full}"
    padding: "2px 10px"
    typography: "{typography.label}"
  badge-warning:
    backgroundColor: "{colors.warning}"
    textColor: "#ffffff"
    rounded: "{rounded.full}"
    padding: "2px 10px"
    typography: "{typography.label}"
  badge-error:
    backgroundColor: "{colors.error}"
    textColor: "#ffffff"
    rounded: "{rounded.full}"
    padding: "2px 10px"
    typography: "{typography.label}"
  table-row:
    padding: "10px 16px"
    borderColor: "{colors.neutral-100}"
  table-row-hover:
    backgroundColor: "{colors.neutral-50}"
  sidebar:
    width: "256px"
    widthCollapsed: "80px"
    backgroundColor: "{colors.surface-light}"
    borderColor: "{colors.neutral-200}"
---

# DotMac Sub Design System

## Overview

DotMac Sub is a multi-tenant subscription management system built for ISPs and fiber network operators. The primary users are **NOC technicians** who monitor and troubleshoot GPON/fiber connections under time pressure, often with multiple systems open.

### Brand Personality

**Precise, confident, quiet.** The interface earns trust through accuracy and speed, not visual noise. The emotional goal: operators feel *in control* — they see what's happening, they know their action succeeded, they never wonder where a number came from.

### Visual Reference

SmartOLT's information architecture (density, tab groupings, KPI strips) rendered with modern Tailwind aesthetics. Keep their pattern library, beat them on typography, spacing, dark mode, and interactive states.

### Anti-References

- Generic AI/SaaS purple gradients — too consumer, not serious
- Stripe/Linear whitespace-heavy minimalism — wastes NOC screen real estate
- Hardware vendor UIs (Huawei iManager, Cisco DNA) — utilitarian but dated
- Decorative illustrations on data pages — reserve for empty states only

---

## Colors

### Primary Palette

The primary color is **teal/cyan** (`#06b6d4`) — professional, technical, distinct from typical SaaS blue. It signals network, connectivity, and primary actions.

| Token | Hex | Usage |
|-------|-----|-------|
| `primary-500` | `#06b6d4` | Primary buttons, links, active states |
| `primary-600` | `#0891b2` | Hover states, gradients |
| `primary-700` | `#0e7490` | Pressed states |

### Accent Palette

The accent is **warm orange** (`#f97316`) — provides visual warmth and draws attention without alarm.

| Token | Hex | Usage |
|-------|-----|-------|
| `accent-500` | `#f97316` | Highlights, attention indicators |
| `accent-600` | `#ea580c` | Hover states |

### Semantic Colors (Contract)

These colors have **fixed meanings** — never use them decoratively:

| Color | Token | Meaning | Examples |
|-------|-------|---------|----------|
| Emerald | `success` | Healthy, online, complete | ONT online, payment received |
| Amber | `warning` | Needs attention, degraded | Low signal, overdue invoice |
| Rose | `error` | Critical, offline, failed | ONT offline, authorization failed |
| Blue | `info` | Network, informational | IP addresses, interface stats |
| Violet | `identity` | People, accounts | Users, subscribers, contacts |
| Pink | `billing` | Financial | Invoices, payments, revenue |
| Purple | `infrastructure` | OLTs, fiber plant | OLT cards, PON ports |

### Dark Mode

Full dark mode support is mandatory. Use slate neutrals:

| Context | Light | Dark |
|---------|-------|------|
| Background | `#f1f5f9` (slate-100) | `#020617` (slate-950) |
| Surface | `#ffffff` | `#0f172a` (slate-900) |
| Border | `#e2e8f0` (slate-200) | `#334155` (slate-700) |
| Text primary | `#0f172a` (slate-900) | `#f8fafc` (slate-50) |
| Text secondary | `#64748b` (slate-500) | `#94a3b8` (slate-400) |

---

## Typography

### Font Families

| Role | Font | Usage |
|------|------|-------|
| Display | **Outfit** | Page titles, headings, hero text |
| Body | **Plus Jakarta Sans** | Body text, labels, UI copy |
| Mono | System monospace | IPs, MACs, serial numbers, code |

### Type Scale

| Level | Size | Weight | Use Case |
|-------|------|--------|----------|
| `display-xl` | 36px | 700 | Dashboard hero numbers |
| `display-lg` | 30px | 700 | Page titles |
| `heading-lg` | 24px | 700 | Section headings |
| `heading-md` | 20px | 600 | Card titles |
| `heading-sm` | 16px | 600 | Subsection headings |
| `body-lg` | 16px | 400 | Prominent body text |
| `body-md` | 14px | 400 | Default body text |
| `body-sm` | 13px | 400 | Secondary text, descriptions |
| `label` | 12px | 500 | Form labels, metadata |
| `caption` | 11px | 600 | Uppercase labels, badges |

### Typography Rules

1. **Outfit** for titles — tight tracking (`-0.02em`), never body text
2. **tabular-nums** for all numbers — IPs, signal values, counts, currency
3. Bold the **value**, dim the **label** — never bold both
4. Uppercase captions get `tracking-[0.05em]` minimum

---

## Layout

### Spacing Scale

Base unit is **4px**. Common values:

| Token | Value | Usage |
|-------|-------|-------|
| `1` | 4px | Tight gaps (icon-to-text) |
| `2` | 8px | Compact spacing |
| `2.5` | 10px | Default row padding |
| `3` | 12px | Section gaps |
| `4` | 16px | Card padding, form gaps |
| `6` | 24px | Section padding |
| `8` | 32px | Page sections |

### Density Philosophy

NOC operators want **more** per screen, not less:

- Table rows: `py-2.5` (10px vertical padding)
- Body text: `text-sm` (14px) as default
- Compact but not crushed — clear section boundaries

### Container

- Max width: `max-w-7xl` (1280px)
- Horizontal padding: `px-4 sm:px-6 lg:px-8`

### Grid Patterns

- Dashboard: 2-4 column responsive grids
- Detail pages: Primary content (2/3) + sidebar (1/3)
- Tables: Full width with horizontal scroll on mobile

---

## Elevation & Depth

### Shadow Scale

| Level | Shadow | Usage |
|-------|--------|-------|
| `sm` | `0 1px 2px rgb(0 0 0 / 0.05)` | Subtle lift |
| `DEFAULT` | `0 1px 3px rgb(0 0 0 / 0.1)` | Cards, dropdowns |
| `md` | `0 4px 6px rgb(0 0 0 / 0.1)` | Elevated cards |
| `lg` | `0 10px 15px rgb(0 0 0 / 0.1)` | Modals, popovers |
| `xl` | `0 20px 25px rgb(0 0 0 / 0.1)` | Focused elements |

### Colored Shadows

Primary buttons use colored shadows for depth:

```
shadow-lg shadow-primary-500/25
```

### Visual Hierarchy

1. **Page background**: Subtle gradient mesh + noise texture
2. **Cards**: White/slate-800 with 1px border
3. **Modals**: White/slate-800 with lg shadow + backdrop blur
4. **Dropdowns**: White/slate-800 with border + shadow

---

## Shapes

### Border Radius Scale

| Token | Value | Usage |
|-------|-------|-------|
| `sm` | 4px | Small elements, badges |
| `md` | 8px | Inputs, small buttons |
| `lg` | 12px | Cards, modals |
| `xl` | 16px | Buttons, large cards |
| `2xl` | 20px | Page headers |
| `3xl` | 24px | Hero cards |
| `full` | 9999px | Pills, avatars |

### Conventions

- Buttons: `rounded-xl` (16px)
- Cards: `rounded-2xl` (20px) to `rounded-3xl` (24px)
- Inputs: `rounded-lg` (12px)
- Badges: `rounded-full`
- Avatars: `rounded-full`

---

## Components

### Page Header

Gradient background card with icon, title, subtitle, and action buttons:

```
rounded-3xl border border-slate-200/70
bg-gradient-to-br from-{color}-50 via-white to-{color2}-50/70
px-6 py-6 shadow-sm
```

### Stats Cards

KPI display with icon, value, label, and optional trend:

- Icon: Gradient background `rounded-2xl`
- Value: `text-2xl font-bold tabular-nums`
- Label: `text-sm text-slate-500`

### Status Badges

Color-coded pills indicating state:

| Status | Color | Text |
|--------|-------|------|
| Online/Active | emerald | white |
| Warning/Degraded | amber | white |
| Offline/Failed | rose | white |
| Pending | blue | white |
| Neutral | slate | slate-700 |

### Tables

- Header: `text-xs uppercase tracking-wider text-slate-500 bg-slate-50`
- Rows: `py-2.5 border-b border-slate-100`
- Hover: `hover:bg-slate-50`
- Numeric columns: `tabular-nums text-right`

### Forms

- Labels: `text-sm font-medium text-slate-700`
- Inputs: `rounded-lg border-slate-300 focus:border-primary-500 focus:ring-primary-500`
- Help text: `text-sm text-slate-500`
- Errors: `text-sm text-rose-600`

### Sidebar Navigation

- Width: 256px (expanded), 80px (collapsed)
- Active item: `bg-primary-50 text-primary-700 border-l-2 border-primary-500`
- Hover: `hover:bg-slate-100`

---

## Do's and Don'ts

### Do

- **Status first**: Online/offline visible within 200ms of looking at any row
- **Use shape + color + position** for status — never color alone
- **Show inline feedback**: Every mutation shows success/failure without page reload
- **Use HTMX swaps** over full page navigation
- **Match dark mode** at author time, not as an afterthought
- **Use tabular-nums** for all numeric data
- **Respect prefers-reduced-motion** — animations are accents, not signal carriers

### Don't

- **Never use semantic colors decoratively** — rose means failure, emerald means success
- **Never bold both label and value** — bold the value, dim the label
- **Never hardcode hex colors** — use Tailwind utilities or CSS variables
- **Never use `| safe` on user content** — XSS vulnerability
- **Never put business logic in routes** — services only
- **Never skip CSRF tokens** in forms
- **Never use string interpolation in Tailwind classes** — gets purged (use safelist)

### Accessibility

- Target: **WCAG AA**
- Text contrast: 4.5:1 minimum
- UI element contrast: 3:1 minimum
- Every icon needs `aria-label` or `sr-only` partner
- Every form control needs a `<label>`
- Focus rings must be visible
- No information conveyed by color alone

---

## Agent Prompt Guide

Quick color references for AI prompts:

| Intent | Use |
|--------|-----|
| Primary action | `primary-500`, `primary-600` |
| Secondary action | `slate-200` border, `slate-700` text |
| Destructive action | `rose-500`, `red-500` |
| Success state | `emerald-500` |
| Warning state | `amber-500` |
| Error state | `rose-500` |
| Network/technical | `blue-500`, `cyan-500` |
| People/identity | `violet-500` |
| Billing/money | `pink-500` |
| Infrastructure | `purple-500` |
| Neutral/metadata | `slate-500` |

### Component Patterns

```
Page header: rounded-3xl, gradient from-{color}-50, icon in rounded-2xl gradient
Cards: rounded-2xl, border-slate-200, bg-white dark:bg-slate-800
Buttons: rounded-xl, gradient backgrounds, shadow-lg shadow-{color}-500/25
Tables: text-sm, py-2.5 rows, tabular-nums for numbers
Badges: rounded-full, px-2.5 py-0.5, text-xs font-medium
```

### Dark Mode Classes

Always pair light and dark:

```
bg-white dark:bg-slate-800
text-slate-900 dark:text-white
border-slate-200 dark:border-slate-700
```
