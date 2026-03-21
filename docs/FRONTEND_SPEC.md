# Frontend Developer Specification

> **Stack:** Jinja2 + HTMX + Alpine.js + Tailwind CSS v4
> **Layout:** Server-rendered templates consuming context dicts from Python web services
> **No REST API consumed by frontend** — the web service context dict IS the interface contract.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [How It Works](#how-it-works)
3. [Admin Layout & Blocks](#admin-layout--blocks)
4. [UI Macro Library](#ui-macro-library)
5. [Design System](#design-system)
6. [Module Page Specs](#module-page-specs)
   - [Dashboard](#dashboard)
   - [Customers](#customers)
   - [Subscribers](#subscribers)
   - [Billing — Invoices](#billing--invoices)
   - [Billing — Payments](#billing--payments)
   - [Catalog — Offers](#catalog--offers)
   - [Catalog — Subscriptions](#catalog--subscriptions)
   - [Network — OLTs](#network--olts)
   - [Network — ONTs](#network--onts)
   - [Provisioning](#provisioning)
   - [Reports](#reports)
   - [VPN Management](#vpn-management)
   - [Resellers](#resellers)
   - [Notifications](#notifications)
   - [System / Admin](#system--admin)
7. [Model Field Reference](#model-field-reference)
8. [HTMX Interaction Map](#htmx-interaction-map)
9. [Form Patterns](#form-patterns)
10. [Template Conventions](#template-conventions)

---

## Architecture Overview

```
Browser Request
    │
    ▼
Web Route (app/web/admin/*.py)     ← thin wrapper, no logic
    │
    ▼
Web Service (app/services/web_*.py) ← builds context dict
    │
    ▼
Jinja2 Template (templates/admin/**/*.html)
    │
    ▼
HTML Response (with HTMX attributes for dynamic behavior)
```

**Key principle:** Routes are thin wrappers. ALL data assembly, filtering, aggregation, and business logic lives in web service files (`app/services/web_*.py`). The template receives a **context dict** — that dict is your "API response".

---

## How It Works

### GET (List/Detail Page)
```python
# Route (you don't write this — it exists)
@router.get("/billing", response_class=HTMLResponse)
def billing_index(request: Request, db: Session = Depends(get_db)):
    context = web_billing_overview.build_invoices_list_data(db, page=1, per_page=25)
    context["request"] = request
    context["active_page"] = "billing"
    return templates.TemplateResponse("admin/billing/index.html", context)
```

### POST (Form Submission)
```python
# Always redirects after POST (POST-Redirect-GET pattern)
@router.post("/billing/invoices/create", response_class=HTMLResponse)
def create_invoice(request: Request, db: Session = Depends(get_db)):
    web_billing_invoices.handle_create(request, db)
    return RedirectResponse(url="/admin/billing", status_code=303)
```

### HTMX Partial
```python
# Returns a fragment, not a full page
@router.get("/dashboard/stats", response_class=HTMLResponse)
def dashboard_stats(request: Request, db: Session = Depends(get_db)):
    context = web_admin_dashboard.dashboard_stats_partial(request, db)
    return templates.TemplateResponse("admin/dashboard/_stats.html", context)
```

---

## Admin Layout & Blocks

**File:** `templates/layouts/admin.html` (extends `base.html`)

### Available Blocks

| Block | Location | Purpose |
|-------|----------|---------|
| `{% block breadcrumbs %}` | Top nav bar, left | Navigation path |
| `{% block page_header %}` | Above content | Title, subtitle, icon, action buttons |
| `{% block content %}` | Main area | Page-specific content |
| `{% block content_container_class %}` | Wrapper | Override container classes |

### Built-in Features (you get these for free)
- **Sidebar** — 240px fixed, collapsible, with nav menu and user profile
- **Dark mode toggle** — Alpine.js store, class-based
- **Global search** — Cmd+K keyboard shortcut
- **Notification dropdown** — Unread count badge, loads via HTMX
- **Mobile responsive** — Hamburger menu, sidebar collapses

### Required Context (auto-injected by middleware)
```python
{
    "request": Request,              # Required by Jinja2
    "current_user": {                # Admin user
        "name": str,
        "email": str,
        "initials": str,
    },
    "sidebar_stats": {               # Sidebar branding
        "app_name": str,
        "sidebar_logo_url": str,
        "sidebar_logo_dark_url": str,
        "notifications_unread": int,
    },
}
```

### Template Skeleton
```html
{% extends "layouts/admin.html" %}
{% from "components/ui/macros.html" import page_header, stats_card, action_button, ... %}

{% block breadcrumbs %}
<a href="/admin/dashboard" class="text-slate-500 hover:text-slate-700">Dashboard</a>
<span class="mx-2 text-slate-400">/</span>
<span class="text-slate-900 dark:text-white">Current Page</span>
{% endblock %}

{% block page_header %}
{{ page_header(title="Page Title", subtitle="Description", icon=icon_users(), color="amber", color2="orange") }}
{% endblock %}

{% block content %}
<!-- Your page content here -->
{% endblock %}
```

---

## UI Macro Library

**File:** `templates/components/ui/macros.html` (~1300 lines, 40+ macros)

Import what you need:
```html
{% from "components/ui/macros.html" import page_header, stats_card, status_badge, data_table, table_head, table_row, row_actions, row_action, empty_state, card, search_input, filter_select, filter_bar, pagination, action_button, tabs, info_row, detail_header, icon_badge, info_banner, type_badge, step_indicator, progress_bar, timeline_item, metric_row, info_grid, section_divider, setting_row, setting_toggle, submit_button, danger_button, warning_button, validated_input, avatar, vendor_badge, connection_status, alert_count_badge, status_filter_card, ambient_background %}
```

### Layout & Page Structure

| Macro | Parameters | Purpose |
|-------|-----------|---------|
| `ambient_background` | `color1, color2, position` | Subtle gradient orb background |
| `page_header` | `title, subtitle, icon, color, color2, actions, breadcrumbs` | Large page title with gradient icon |
| `detail_header` | `title, subtitle, icon, color, color2, back_href, status` | Detail page header with back button |
| `card` | `title, icon, color, color2, actions, padding` | Generic card container |
| `filter_bar` | (caller block) | Container for search/filter controls |
| `tabs` | `items, active, color` | Tab navigation |
| `section_divider` | `label` | Horizontal divider with optional label |
| `info_grid` | (caller block) | Two-column key-value grid |

### Data Display

| Macro | Parameters | Purpose |
|-------|-----------|---------|
| `stats_card` | `label, value, icon, color, color2, href, change, change_type` | KPI metric card with change indicator |
| `status_badge` | `status, variant, pulse, size, show_icon` | Accessible status pill (icon + color) |
| `type_badge` | `type, size` | Person/Organization badge |
| `info_row` | `label, value, icon` | Key-value detail row |
| `metric_row` | `label, value, color, mono` | Label-value with colored dot |
| `progress_bar` | `value, max, label, show_text, size, color` | Horizontal bar with auto-coloring |
| `timeline_item` | `title, description, time, color, is_last` | Vertical timeline entry |
| `connection_status` | `status, label, last_seen` | Online/offline dot indicator |
| `alert_count_badge` | `count, color` | Count pill, hidden when 0 |
| `icon_badge` | `icon, color, color2, size` | Small gradient icon container |
| `avatar` | `name, type, size, color` | User avatar with initials |
| `vendor_badge` | `vendor, size` | Vendor/provider badge |

### Tables

| Macro | Parameters | Purpose |
|-------|-----------|---------|
| `data_table` | `title, icon, color, count, actions` | Table wrapper with header |
| `table_head` | `columns` | Styled `<thead>` |
| `table_row` | `color` | `<tr>` with hover gradient |
| `row_actions` | (caller block) | Action button container in last column |
| `row_action` | `href, icon, title, color, hx_attrs` | Individual row action button |
| `empty_state` | `title, message, icon, color, color2, action_label, action_href, colspan` | Empty table state with CTA |
| `pagination` | `page, total_pages, total, per_page, base_url, hx_target, color, extra_params` | HTMX-powered pagination |
| `status_filter_card` | `label, value, icon, href, color, color2, is_active` | Clickable filter card |

### Buttons & Forms

| Macro | Parameters | Purpose |
|-------|-----------|---------|
| `action_button` | `label, href, icon, variant, color, color2, size, hx_attrs` | Primary/secondary/ghost/danger button |
| `submit_button` | `label, loading_label, icon, color, size` | Form submit with loading state |
| `danger_button` | `label, confirm_title, confirm_message, action_url, method, size, icon` | Delete with confirmation modal |
| `warning_button` | `label, confirm_title, confirm_message, action_url, method, size, icon` | Warning action with confirmation |
| `search_input` | `name, value, placeholder, color, hx_attrs` | Search input with icon |
| `filter_select` | `name, options, value, label, color, attrs` | Styled select dropdown |
| `validated_input` | `name, label, type, value, placeholder, required, validation_url, help_text, icon` | Input with real-time validation |
| `setting_row` | `label, description, source` | Settings page row |
| `setting_toggle` | `name, value` | Toggle switch |
| `step_indicator` | `steps, current, color` | Multi-step wizard indicator |
| `info_banner` | `message, icon, color, actions` | Contextual info banner |

### Icon Macros

All accept optional `class` parameter for sizing.

**Small icons** (default `h-4 w-4`): `icon_plus`, `icon_view`, `icon_edit`, `icon_delete`, `icon_calendar`, `icon_clock`, `icon_check`, `icon_x`, `icon_link`, `icon_mail`, `icon_refresh`, `icon_filter`, `icon_search`, `icon_download`, `icon_upload`

**Large icons** (default `h-6 w-6 text-white`): `icon_users`, `icon_user`, `icon_server`, `icon_network`, `icon_location`, `icon_ticket`, `icon_invoice`, `icon_check_circle`, `icon_lightning`, `icon_bell`, `icon_clipboard`, `icon_archive`, `icon_credit_card`, `icon_trending_up`, `icon_chart_bar`, `icon_shield`, `icon_cog`, `icon_puzzle`, `icon_heart`, `icon_cpu`, `icon_warning`, `icon_building`, `icon_database`

---

## Design System

### Module Accent Colors

| Module | color | color2 | Used In |
|--------|-------|--------|---------|
| Subscribers/Customers | `amber` | `orange` | page_header, action_button, filter_bar |
| Billing/Invoices | `emerald` | `teal` | Invoice detail, payment status, KPIs |
| Catalog/Offers | `violet` | `purple` | Plan cards, pricing, subscription stats |
| Network/Infrastructure | `blue` | `indigo` | Device status, topology, IP management |
| Speed Tests | `cyan` | `sky` | Speedtest results, bandwidth |
| Provisioning | `amber` | `orange` | Service orders, workflow steps |
| System/Admin | `slate` | `gray` | Settings, users, roles |
| VPN/WireGuard | `cyan` | `teal` | VPN status, peers |
| Reports/Analytics | `teal` | `emerald` | Charts, KPIs |
| GIS/Mapping | `green` | `emerald` | Map markers, coverage |
| Notifications | `rose` | `pink` | Alerts, on-call |
| Integrations | `indigo` | `blue` | Connectors, webhooks |

### Status Badge Variants

| variant | When to use | Visual |
|---------|-------------|--------|
| `"active"` | Active subscriber/subscription | Indigo/blue gradient |
| `"online"` | Device connected | Emerald with pulse |
| `"offline"` | Device disconnected | Red/rose gradient |
| `"pending"` | Awaiting action | Amber semi-transparent |
| `"paid"` | Invoice paid | Emerald semi-transparent |
| `"overdue"` | Past-due invoice | Red semi-transparent |
| `"draft"` | Not submitted | Slate |
| `"warning"` | Needs attention | Amber/orange gradient |
| `"error"` | Failed operation | Red semi-transparent |
| `"info"` | Informational | Blue semi-transparent |
| `"maintenance"` | Under maintenance | Amber |
| `"decommissioned"` | Retired device | Slate |

### Typography

| Element | Classes |
|---------|---------|
| Display font | `font-display` (Outfit, `letter-spacing: -0.02em`) |
| Body font | `font-body` / `font-sans` (Plus Jakarta Sans) |
| Page heading | `font-display font-bold tracking-tight` |
| Body text | `text-sm` (dense data) or `text-base` (readable) |
| Financial values | `tabular-nums font-mono` |
| IP/MAC addresses | `font-mono` |
| Technical identifiers | `font-mono text-[13px]` for ONT serials, PPPoE usernames, VLAN IDs, account numbers, port references |
| Labels | `text-sm font-medium text-slate-700 dark:text-slate-300` |
| Helper text | `text-xs text-slate-500 dark:text-slate-400` |

### Dark Mode

Class-based toggle. Always pair light/dark:
```html
bg-white dark:bg-slate-800
text-slate-900 dark:text-white
border-slate-200 dark:border-slate-700
bg-slate-50 dark:bg-slate-900      <!-- alternating rows -->
```

---

## ISP UX Conventions

These conventions make the design system fit ISP operations instead of a generic back-office app.

### Identity Model

- Subscriber identity is dual-track:
  - human identity: name, phone, address, zone
  - service identity: account number, subscriber code, PPPoE username, ONT serial, OLT/PON location, IP, MAC
- Detail pages should show both tracks in the summary region when relevant.
- Technical identifiers should be easy to copy and visually distinct from narrative metadata.

### Search Model

- Subscriber and network search inputs should support:
  - name
  - phone
  - account number
  - invoice number
  - PPPoE username
  - ONT serial
  - IP / MAC
  - OLT or port reference
- Placeholder copy should teach supported identifiers instead of using generic text like `Search...`.

### State Semantics

- `online` is not the same as `healthy`.
- `offline` is not the same as `degraded`.
- Subscriber service state, payment state, and device state should not be collapsed into one badge.
- When a page mixes billing and network information, keep those states visually separate and explicitly labeled.

### NOC / Outage Presentation

- Favor grouped impact surfaces:
  - affected zone / site
  - OLT or PON blast radius
  - affected subscriber count
  - probable cause / primary reason
- Alarm feeds are supporting detail, not the only way to understand impact.
- Current impact belongs above historical trend.

### Provisioning / Activation Presentation

- Provisioning screens should show prerequisites before execution.
- Preview / dry-run output is part of the work surface, not an advanced hidden panel.
- Async execution needs stable progress states: pending, running, failed, retryable, completed.

### Map / Topology Presentation

- Maps must pair with a synchronized list, table, or detail rail.
- Marker color cannot be the only severity cue.
- Clusters should expose count and severity mix before drill-in.

## Module Page Specs

Each spec documents: URL, template path, context dict shape, and HTMX partials.

---

### Dashboard

#### `GET /admin/dashboard`
**Template:** `admin/dashboard/index.html`

```python
{
    # ── KPI Stats (compact decision layer; 4-6 items shown) ──
    "stats": {
        "total_subscribers": int,
        "active_subscribers": int,
        "subscribers_change": int,          # vs previous period
        "monthly_revenue": float,
        "mrr": float,                       # Monthly Recurring Revenue
        "arpu": float,                      # Average Revenue Per User
        "revenue_change": float,
        "system_uptime": float,             # percentage
        "ar_current": float,               # Accounts Receivable buckets
        "ar_30": float,
        "ar_60": float,
        "ar_90": float,
        "suspended_accounts": int,
        "orders_new": int,
        "orders_qualification": int,
        "orders_scheduled": int,
        "orders_in_progress": int,
        "orders_pending_activation": int,
        "orders_completed_today": int,
        "olts_online": int,
        "olts_total": int,
        "onts_active": int,
        "onts_total": int,
        "alarms_critical": int,
        "alarms_major": int,
        "alarms_minor": int,
        "alarms_warning": int,
        "bandwidth_current": float,
        "bandwidth_peak": float,
        "bandwidth_capacity": float,
        "jobs_completed": int,
        "jobs_total": int,
        "techs_active": int,
        "churn_rate": float,
    },

    # ── Exception / Attention Layer ──
    "attention_items": list[dict],          # [{label, href, severity}]
    "pending_orders": int,
    "total_alarms": int,

    # ── Supporting Operational Summaries ──
    "online_count": int,                    # active RADIUS sessions
    "monitoring_summary": {
        "devices_online": int,
        "devices_offline": int,
        "devices_degraded": int,
        "devices_total": int,
    },
    "onu_summary": {
        "online": int,
        "offline": int,
        "low_signal": int,
        "total": int,
    },

    # ── Work Surface Data ──
    "recent_activity": list,                # raw audit events if needed elsewhere
    "recent_activities": list[dict],        # [{type, message, detail, time}]
    "recent_subscribers": list,             # newest signups

    # ── Lower Live Partial(s) ──
    "server_health": dict,                  # CPU, memory, disk metrics
    "server_health_status": dict,           # overall status + issues

    # ── Permission Gates ──
    "show_financials": bool,
    "show_network": bool,
    "show_subscribers": bool,

    # ── Standard ──
    "now": datetime,
    "active_page": "dashboard",
    "active_menu": "dashboard",
}
```

Dashboard implementation notes:
- The first viewport should expose one true work surface such as priority activity, pending installs, outage impact, or collections risk.
- Launchpads are support navigation, not the primary payload.
- If network and billing metrics both appear, keep them grouped by task instead of mixing them into one undifferentiated KPI wall.

#### Dashboard Composition Rules

- Use one primary action in the page header.
- Keep the first decision layer to 4-6 KPI tiles.
- Show only exception-based items in the attention banner.
- Group quick links by workflow (`Operations`, `Network`, `Control`), not by every available module.
- The first work surface should be operational content such as activity, queue state, or recent records.
- Lower HTMX partials like server health belong below or beside the main work surface, not above it.

#### HTMX Partials

| Endpoint | Template | Trigger |
|----------|----------|---------|
| `GET /admin/dashboard/stats` | `_stats.html` | Auto-refresh polling for KPI strip |
| `GET /admin/dashboard/activity` | `_activity.html` | Auto-refresh polling for activity work surface |
| `GET /admin/dashboard/server-health` | `_server_health.html` | Auto-refresh polling for lower live status widget |

---

### Customers

#### `GET /admin/customers`
**Template:** `admin/customers/index.html`

```python
{
    "customers": list[dict],            # Each:
    #   {
    #       "id": str (UUID),
    #       "type": "person" | "organization",
    #       "name": str,
    #       "email": str | None,
    #       "phone": str | None,
    #       "is_active": bool,
    #       "created_at": datetime,
    #       "raw": Subscriber | Organization,  # full ORM object
    #   }
    "stats": {
        "total_customers": int,
        "total_people": int,
        "total_organizations": int,
    },
    "page": int,
    "per_page": int,
    "total": int,
    "total_pages": int,
    "search": str | None,
    "customer_type": str | None,        # filter: "person" | "organization" | None
    "active_page": "customers",
}
```

#### `GET /admin/customers/{type}/{id}` (Person Detail)
**Template:** `admin/customers/detail.html`

```python
{
    "customer": Subscriber,             # full ORM object
    "customer_type": "person",
    "customer_name": str,

    # ── Related Records ──
    "subscribers": list[Subscriber],
    "accounts": list[Subscriber],
    "subscriptions": list[Subscription],
    "account_lookup": dict[str, Subscriber],  # account_id → Subscriber
    "invoices": list[Invoice],
    "payments": list[Payment],
    "notifications": list[Notification],

    # ── Address & Location ──
    "addresses": list[Address],
    "primary_address": Address | None,
    "map_data": {                       # or None
        "center": [lat, lon],
        "geojson": {"type": "FeatureCollection", "features": [...]},
    },
    "geocode_target": dict | None,

    "contacts": list[dict],             # empty for persons

    # ── Statistics ──
    "stats": {
        "total_subscribers": int,
        "total_subscriptions": int,
        "active_subscriptions": int,
        "balance_due": float,
        "total_addresses": int,
        "total_contacts": int,
    },
    "financials": {
        "total_invoiced": float,
        "total_paid": float,
        "overdue_invoices": int,
        "last_payment": Payment | None,
        "last_invoice": Invoice | None,
        "monthly_recurring": float,
    },

    "has_active_subscribers": bool,
    "has_any_subscribers": bool,
    "activity_items": list[dict],       # [{type, title, description, timestamp}]
    "customer_user_access": dict,       # portal login state
    "active_page": "customers",
}
```

#### `GET /admin/customers/{type}/{id}` (Organization Detail)
Same shape as person, plus:
```python
{
    "customer": Organization,
    "customer_type": "organization",
    "contacts": list[dict],             # [{id, first_name, last_name, role, title, is_primary, email, phone}]
}
```

#### Form Actions

| Method | URL | Action | Redirect |
|--------|-----|--------|----------|
| POST | `/admin/customers/new` | Create person or org | `/admin/customers/{type}/{id}` |
| POST | `/admin/customers/{type}/{id}/edit` | Update | Back to detail |
| DELETE | `/admin/customers/{type}/{id}` | Deactivate/delete | `/admin/customers` |
| POST | `/admin/customers/addresses` | Add address | Back to detail |
| POST | `/admin/customers/contacts` | Add contact | Back to detail |
| POST | `/admin/customers/bulk/status` | Bulk status change | `/admin/customers` |
| POST | `/admin/customers/bulk/delete` | Bulk delete | `/admin/customers` |

---

### Subscribers

#### `GET /admin/subscribers/{id}`
**Template:** `admin/subscribers/detail.html`

This is the **richest detail page** in the system:

```python
{
    "subscriber": Subscriber,

    # ── Service Records ──
    "accounts": list[Account],
    "subscriptions": list[Subscription],        # active, max 10
    "all_subscriptions": list[Subscription],    # all statuses
    "online_status": dict[str, bool],           # subscription_id → is_online
    "invoices": list[Invoice],
    "payments": list[Payment],
    "dunning_cases": list[DunningCase],
    "service_orders": list[ServiceOrder],
    "notifications": list[Notification],

    # ── Financial Stats ──
    "stats": {
        "monthly_bill": float,
        "balance_due": float,
        "credit_issued": float,
        "current_balance": float,
        "has_credit_adjustment": bool,
        "data_usage": str,
    },

    # ── Address & Location ──
    "addresses": list[Address],
    "primary_address": Address | None,
    "map_data": dict | None,
    "geocode_target": dict | None,

    # ── Contacts & Org ──
    "contacts": list[dict],                     # [{id, type, label, value, is_primary}]
    "organization_members": list[dict],         # [{id, name, email, is_active, is_current}]

    # ── Speed Tests ──
    "speedtests": list[SpeedTestResult],
    "speedtest_performance_rows": list[dict],   # [{test, down_ratio_pct, up_ratio_pct, is_underperforming}]
    "speedtest_chart": {
        "labels": list[str],
        "download": list[float],
        "upload": list[float],
    },
    "speedtest_plan": {
        "download_mbps": float,
        "upload_mbps": float,
    },
    "speedtest_underperforming_count": int,

    # ── Equipment ──
    "equipment": list[dict],                    # [{type, model, serial, online, detail_url, tr069_url}]
    "primary_ont_url": str | None,
    "primary_ont_tr069_url": str | None,

    # ── Enrichment ──
    "reseller_name": str | None,
    "last_online": str | None,
    "billing_email": str | None,
    "gps_coordinates": str | None,
    "access_credentials": list[AccessCredential],
    "nas_device_names": dict[str, str],         # subscription_id → NAS name
    "comms_email_count": int,
    "comms_sms_count": int,

    # ── Billing Config ──
    "billing_config": {
        "category": str | None,
        "billing_day": int | None,
        "payment_due_days": int | None,
        "grace_period_days": int | None,
        "min_balance": float | None,
        "billing_enabled": bool,
        "blocking_period_days": int,
        "deactivation_period_days": int,
        "auto_create_invoices": bool,
        "send_billing_notifications": bool,
        "next_block_at": datetime | None,
        "next_block_label": str,
    },

    # ── Timeline ──
    "timeline": list[dict],                     # [{id, type, title, detail, is_comment, is_todo, is_completed, attachments, time}]

    # ── Form Helpers ──
    "offers": list[CatalogOffer],
    "subscription_statuses": list[str],
    "contract_terms": list[str],
    "subscriber_user_access": dict,

    "active_page": "subscribers",
}
```

---

### Billing — Invoices

#### `GET /admin/billing`
**Template:** `admin/billing/index.html`

```python
{
    "invoices": list[Invoice],                  # ORM objects

    "status_totals": {                          # per-status aggregation
        "draft":          {"count": int, "amount": float},
        "issued":         {"count": int, "amount": float},
        "partially_paid": {"count": int, "amount": float},
        "paid":           {"count": int, "amount": float},
        "overdue":        {"count": int, "amount": float},
        "void":           {"count": int, "amount": float},
        "all":            {"count": int, "amount": float, "due_total": float, "received_total": float},
    },

    # ── Pagination ──
    "page": int,
    "per_page": int,
    "total": int,
    "total_pages": int,

    # ── Filters ──
    "account_id": str | None,
    "selected_partner_id": str | None,
    "partner_options": list[dict],              # [{id, name}]
    "status": str | None,
    "proforma_only": bool,
    "proforma_summary": {"count": int},
    "customer_ref": str | None,
    "search": str | None,
    "date_range": str | None,

    "active_page": "billing",
}
```

#### `GET /admin/billing/invoices/{id}`
**Template:** `admin/billing/invoices/detail.html`

```python
{
    "invoice": Invoice,                         # ORM object with .lines, .account, etc.
    "tax_rates": list[TaxRate],
    "credit_notes": list[CreditNote],
    "activities": list[dict],                   # [{title, description, occurred_at}]
    "pdf_export": PdfExport | None,
    "is_proforma": bool,
    "active_page": "billing",
}
```

#### Invoice Form (New)

```python
{
    "tax_rates": list[TaxRate],
    "tax_rates_json": list[dict],               # [{id, name, rate}] for Alpine.js
    "invoice_config": {                         # Alpine.js x-data payload
        "accountId": str,
        "invoiceNumber": str,
        "status": str,
        "currency": str,
        "issuedAt": str,                        # "YYYY-MM-DD"
        "dueAt": str,
        "memo": str,
        "taxRates": list[dict],
        "lineItems": list[dict],
        "invoiceId": str,
        "paymentTermsDays": int,
    },
    "default_issue_date": str,
    "default_due_date": str,
    "default_currency": str,
    "account_locked": bool,
    "account_label": str | None,
    "selected_account_id": str | None,
}
```

#### AR Aging Report

```python
{
    "buckets": {                                # Aging buckets
        "current": list[Invoice],
        "1_30": list[Invoice],
        "31_60": list[Invoice],
        "61_90": list[Invoice],
        "90_plus": list[Invoice],
    },
    "totals": {"current": float, "1_30": float, ...},
    "counts": {"current": int, "1_30": int, ...},
    "bucket_rows": {                            # Enriched rows
        "current": [{"invoice": Invoice, "account_label": str, "last_payment_at": date | None}],
    },
    "bucket_order": list[dict],                 # [{key, label, amount, count, is_selected}]
    "top_debtors": list[dict],                  # [{account_id, account_label, amount}]
    "aging_trend": {                            # Chart data
        "labels": list[str],                    # ["Jan 2024", ...]
        "series": {"current": list[float], "1_30": list[float], ...},
    },
}
```

---

### Billing — Payments

#### `GET /admin/billing/payments`
**Template:** `admin/billing/payments/index.html`

```python
{
    "payments": list[Payment],                  # enriched with display_number, display_method, narration

    "status_totals": {
        "succeeded":          {"count": int, "amount": float},
        "pending":            {"count": int, "amount": float},
        "failed":             {"count": int, "amount": float},
        "refunded":           {"count": int, "amount": float},
        "partially_refunded": {"count": int, "amount": float},
        "canceled":           {"count": int, "amount": float},
        "all":                {"count": int, "amount": float},
    },

    # ── Pagination ──
    "page": int,
    "per_page": int,
    "total": int,
    "total_pages": int,
    "total_balance": float,
    "active_count": int,
    "suspended_count": int,

    # ── Filters ──
    "customer_ref": str | None,
    "selected_partner_id": str | None,
    "partner_options": list[dict],              # [{id, name}]
    "status": str | None,
    "method": str | None,
    "search": str | None,
    "date_range": str | None,
    "unallocated_only": bool,

    "active_page": "payments",
}
```

#### Payment Form (New)

```python
{
    "prefill": {
        "invoice_id": str | None,
        "invoice_number": str | None,
        "amount": float | None,
        "currency": str | None,
        "status": str | None,                   # "succeeded"
        "account_id": str | None,
    },
    "selected_account": Subscriber | None,
    "invoice_label": str | None,
    "balance_value": str | None,
    "balance_display": str | None,
    "collection_accounts": list[CollectionAccount],
    "invoices": list[Invoice],                  # open invoices only
}
```

---

### Catalog — Offers

#### `GET /admin/catalog/offers`
**Template:** `admin/catalog/offers/index.html`

```python
{
    "offers": list[CatalogOffer],

    "offer_subscription_counts": dict[str, int],        # offer_id → total count
    "offer_active_subscription_counts": dict[str, int], # offer_id → active count
    "offer_plan_metadata": dict[str, dict],             # offer_id → {plan_kind, ip_block_size}

    # ── Enum Lists (for filter dropdowns & forms) ──
    "service_types": list[str],
    "access_types": list[str],
    "price_bases": list[str],
    "billing_cycles": list[str],
    "contract_terms": list[str],
    "offer_statuses": list[str],
    "plan_categories": list[str],
    "radius_profiles": list[RadiusProfile],

    # ── Filters ──
    "status": str | None,
    "plan_kind": str,
    "plan_category": str,
    "search": str | None,

    # ── Pagination ──
    "page": int,
    "per_page": int,
    "total": int,
    "total_pages": int,

    "active_page": "catalog",
}
```

#### Offer Form (Create/Edit)

```python
{
    "offer": dict,                              # form data (existing values or defaults)

    # ── Dropdown Options ──
    "region_zones": list[RegionZone],
    "all_offers": list[CatalogOffer],
    "usage_allowances": list[UsageAllowance],
    "sla_profiles": list[SlaProfile],
    "radius_profiles": list[RadiusProfile],
    "policy_sets": list[PolicySet],
    "add_ons": list[AddOn],
    "addon_links_map": dict[str, dict],         # addon_id → {is_required, min_quantity, max_quantity}

    # ── Enum Options ──
    "service_types": list[str],
    "access_types": list[str],
    "price_bases": list[str],
    "billing_cycles": list[str],
    "billing_modes": list[str],
    "contract_terms": list[str],
    "offer_statuses": list[str],
    "price_units": list[str],
    "price_types": list[str],                   # ["recurring", "one_time"]
    "guaranteed_speed_types": list[str],
    "plan_categories": list[str],
    "plan_kinds": list[str],                    # ["standard", "ip_address", "device_replacement"]
    "ip_block_sizes": list[str],                # ["/32", "/30", ...]

    "action_url": str,
    "error": str | None,
}
```

#### Plan Usage Chart Data (HTMX)

```python
{
    "labels": list[str],                        # date labels
    "total_counts": list[int],
    "active_counts": list[int],
    "total_now": int,
    "active_now": int,
    "max_total": int,
    "avg_total": float,
    "period": str,                              # "daily" | "weekly" | "monthly"
}
```

---

### Catalog — Subscriptions

#### `GET /admin/catalog/subscriptions`
**Template:** `admin/catalog/subscriptions/index.html`

```python
{
    "subscriptions": list[Subscription],
    "offers": list[CatalogOffer],               # active only (for filter dropdown)
    "status": str | None,
    "page": int,
    "per_page": int,
    "total": int,
    "total_pages": int,
    "active_page": "catalog",
}
```

#### Subscription Form

```python
{
    "subscription": dict,                       # form data

    # ── Related Records ──
    "accounts": list[SubscriberAccount],
    "offers": list[CatalogOffer],
    "nas_devices": list[NasDevice],
    "router_devices": list[NasDevice],
    "ipv4_pools": list[IpPool],
    "ipv4_blocks": list[dict],                  # [{id, pool_id, pool_name, cidr, available_count, available_ips, display}]
    "radius_profiles": list[RadiusProfile],

    # ── Enum Options ──
    "subscription_statuses": list[str],
    "billing_modes": list[str],
    "contract_terms": list[str],

    # ── Display Helpers ──
    "subscriber_label": str,
    "selected_router_label": str,
    "current_service_login": str,
    "current_service_password": str,
    "credential_targets": {
        "email": list[str],
        "sms": list[str],
    },
    "billing_mode_help_text": str,
    "billing_mode_prepaid_notice": str,
    "billing_mode_postpaid_notice": str,

    "action_url": str,
    "error": str | None,
}
```

---

### Network — OLTs

#### `GET /admin/network/olts`
**Template:** `admin/network/olts/index.html`

OLT listing uses the core device listing context — devices filtered to OLT type. Key fields per device:

```python
# Each OLT in the list:
{
    "id": str (UUID),
    "name": str,
    "hostname": str | None,
    "vendor": str | None,
    "model": str | None,
    "mgmt_ip": str | None,
    "status": DeviceStatus,                     # active | inactive | maintenance | retired
    "firmware_version": str | None,
    "snmp_enabled": bool,
    "created_at": datetime,
    # Relationships:
    "pon_ports": list[PonPort],
}
```

#### OLT TR-069 Profiles Context

```python
(
    ok: bool,
    message: str,
    profiles_data: list[dict],                  # [{profile_id, name, acs_url, acs_username, inform_interval, binding_count}]
    {
        "acs_prefill": {acs_url, acs_username, acs_name},
        "onts": list[dict],                     # [{id, serial_number, board, port, onu_index, name, online, subscriber_name}]
    }
)
```

---

### Network — ONTs

#### ONT Form Dependencies

```python
{
    "onu_types": list,
    "olt_devices": list[OLTDevice],
    "vlans": list[Vlan],
    "zones": list,
    "splitters": list[Splitter],
    "speed_profiles_download": list[SpeedProfile],
    "speed_profiles_upload": list[SpeedProfile],
    "pon_types": list[str],                     # PonType enum values
}
```

#### ONT Bulk Action Result

```python
{
    "succeeded": int,
    "failed": int,
    "skipped": int,
    "total": int,
    "results": list[dict],                      # [{ont_id, success, message}]
}
```

---

### Provisioning

#### `GET /admin/provisioning/bulk-activate`
**Template:** `admin/provisioning/bulk_activate.html`

```python
{
    # ── Tab & Filter Options ──
    "tabs": list[str],                          # ["internet", "recurring", "bundle"]
    "tab": str,
    "offers": list[CatalogOffer],
    "resellers": list[Reseller],
    "pop_sites": list[PopSite],
    "nas_devices": list[NasDevice],
    "subscriber_statuses": list[str],
    "jobs": list[dict],                         # recent job history

    "active_page": "provisioning",
}
```

#### Bulk Activate Preview (HTMX)

```python
{
    "rows": list[dict],                         # Each:
    #   {
    #       "subscriber_id": str,
    #       "subscriber_name": str,
    #       "subscriber_email": str,
    #       "subscriber_status": str,
    #       "existing_subscription_id": str,
    #       "existing_offer_name": str,
    #       "existing_subscription_status": str,
    #       "action": "create" | "update" | "skip_active_exists",
    #       "reason": str,
    #   }
    "total_matches": int,
    "shown": int,
    "counts": {"create": int, "update": int, "skip": int},
}
```

#### Job Status

```python
{
    "job_id": str,
    "status": "queued" | "running" | "completed" | "partial" | "failed",
    "progress_percent": int,                    # 0-100
    "queued_at": str,                           # ISO datetime
    "started_at": str | None,
    "completed_at": str | None,
    "error": str | None,
    "result": dict | None,
    "counts": {"activated": int, "failed": int, "skipped": int},
    "total_matches": int,
}
```

---

### Reports

#### `GET /admin/reports`
**Template:** `admin/reports/index.html`

Tabs: Network, Revenue, Subscribers, Churn, Technician

#### Network Report

```python
{
    "olts": list[OLT],
    "total_olts": int,
    "active_olts": int,
    "total_onts": int,
    "connected_onts": int,
    "recent_ont_activity": list[ONT],
    "pool_data": list[dict],                    # [{name, cidr, used_count, total_count}]
    "used_ips": int,
    "total_ips": int,
    "ip_pool_usage": float,                     # percentage
    "active_vlans": int,
}
```

#### Revenue Report

```python
{
    "total_revenue": Decimal,
    "revenue_growth": float,
    "recurring_revenue": Decimal,
    "outstanding_amount": Decimal,
    "outstanding_count": int,
    "collection_rate": float,                   # percentage
    "recent_payments": list[Payment],
}
```

#### Subscribers Report

```python
{
    "total_subscribers": int,
    "subscriber_growth": float,
    "new_this_month": int,
    "active_subscribers": int,
    "suspended_subscribers": int,
    "active_rate": float,
    "status_breakdown": dict[str, int],         # {status_name: count}
    "recent_subscribers": list[Subscriber],
}
```

#### Churn Report

```python
{
    "churn_rate": float,
    "retention_rate": float,
    "cancelled_count": int,
    "at_risk_count": int,
    "churn_reasons": {"price": int, "service_quality": int, "moved": int, "competitor": int},
    "recent_cancellations": list[Subscriber],
}
```

---

### VPN Management

#### `GET /admin/vpn`
**Template:** `admin/vpn/index.html`

```python
{
    # ── Unified Dashboard ──
    "wireguard": dict,                          # from web_vpn_servers
    "openvpn_clients": list[dict],              # [{id, name, client_ip, is_connected, connected_since, rx_bytes, tx_bytes, latency_ms}]
    "openvpn_config": dict,                     # {remote_host, remote_port, proto, server_subnet}

    "connections": list[dict],                  # Unified view:
    #   {
    #       "protocol": "wireguard" | "openvpn",
    #       "id": str,
    #       "name": str,
    #       "server_name": str,
    #       "status": "up" | "down",
    #       "last_handshake_at": datetime | None,
    #       "uptime_seconds": int | None,
    #       "rx_bytes": int,
    #       "tx_bytes": int,
    #       "latency_ms": int | None,
    #       "address": str,
    #   }

    "summary": {
        "total": int,
        "active": int,
        "down": int,
        "wireguard": int,
        "openvpn": int,
    },

    "alerts": list[dict],                       # [{id, protocol, tunnel, severity, message, created_at}]

    "active_page": "vpn",
}
```

---

### Resellers

#### `GET /admin/resellers/{id}`
**Template:** `admin/resellers/detail.html`

```python
{
    "reseller": Reseller,                       # ORM object
    "reseller_subscribers": list[ResellerUser], # paginated
    "reseller_subscribers_total": int,
    "page": int,
    "per_page": int,
    "total_pages": int,
    "active_page": "resellers",
}
```

---

### Notifications

#### `GET /admin/notifications` (Dropdown partial)
**Template:** `admin/notifications/_menu.html`

```python
{
    "notifications": list[Notification],        # max 10, ORM objects
}
```

---

### System / Admin

System has 20+ web services covering: Users, Roles, Permissions, API Keys, Settings, Audit Logs, Health, Logs, Company Info, Scheduler, Webhooks, Import/Export, DB Inspector.

Each follows the same pattern — context dict with list data, form helpers, and enum options. Key entry points:

| URL | Purpose | Key Context |
|-----|---------|-------------|
| `/admin/system/users` | User list | users, roles, page, total |
| `/admin/system/users/{id}` | User detail | user, roles, permissions, activity |
| `/admin/system/roles` | Role list | roles, permission_groups |
| `/admin/system/settings` | Settings hub | domains, settings_by_domain |
| `/admin/system/audit` | Audit log | events, filters, page |
| `/admin/system/health` | System health | cpu, memory, disk, services |

---

## Model Field Reference

These are the ORM objects your templates receive. Access fields with dot notation: `{{ invoice.total }}`.

### Subscriber

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | Internal, use for URLs |
| `first_name` | str | |
| `last_name` | str | |
| `display_name` | str \| None | Preferred over first+last when set |
| `email` | str | |
| `phone` | str \| None | E.164 format |
| `status` | SubscriberStatus | Enum: new, active, suspended, disabled, canceled, delinquent |
| `subscriber_number` | str \| None | Human-readable ID |
| `account_number` | str \| None | |
| `is_active` | bool | |
| `billing_enabled` | bool | |
| `billing_day` | int \| None | Day of month (1-28) |
| `mrr_total` | Decimal \| None | Monthly recurring revenue — `tabular-nums font-mono` |
| `address_line1` | str \| None | |
| `city` | str \| None | |
| `region` | str \| None | |
| `country_code` | str \| None | 2-char ISO |
| `created_at` | datetime | Relative for < 7 days |
| `updated_at` | datetime | |
| `.subscriptions` | list[Subscription] | Relationship |
| `.organization` | Organization \| None | Relationship |

### Invoice

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | |
| `invoice_number` | str \| None | Human-readable |
| `status` | InvoiceStatus | Enum: draft, issued, partially_paid, paid, void, overdue |
| `currency` | str | 3-char, default "NGN" |
| `subtotal` | Decimal | `tabular-nums font-mono` |
| `tax_total` | Decimal | |
| `total` | Decimal | |
| `balance_due` | Decimal | |
| `issued_at` | datetime \| None | |
| `due_at` | datetime \| None | |
| `paid_at` | datetime \| None | |
| `memo` | str \| None | |
| `is_proforma` | bool | |
| `.account` | Subscriber | The billed subscriber |
| `.lines` | list[InvoiceLine] | Line items |

### Payment

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | |
| `amount` | Decimal | `tabular-nums font-mono` |
| `currency` | str | |
| `status` | PaymentStatus | Enum: pending, succeeded, failed, refunded, partially_refunded, canceled |
| `paid_at` | datetime \| None | |
| `receipt_number` | str \| None | |
| `external_id` | str \| None | Provider reference |
| `memo` | str \| None | |
| `.account` | Subscriber | |
| `.payment_method` | PaymentMethod \| None | |

### CatalogOffer

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | |
| `name` | str | |
| `code` | str \| None | Short code |
| `service_type` | ServiceType | Enum: residential, business |
| `plan_category` | PlanCategory | Enum: internet, recurring, one_time, bundle |
| `billing_cycle` | BillingCycle | Enum: daily, weekly, monthly, annual |
| `speed_download_mbps` | int \| None | Display as "100 Mbps" |
| `speed_upload_mbps` | int \| None | Display as "50 Mbps" |
| `status` | OfferStatus | Enum: active, inactive, archived |
| `.prices` | list[OfferPrice] | Pricing tiers |
| `.subscriptions` | list[Subscription] | |

### Subscription

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | |
| `status` | SubscriptionStatus | Enum: pending, active, suspended, stopped, disabled, archived, canceled, expired |
| `billing_mode` | BillingMode | Enum: prepaid, postpaid |
| `contract_term` | ContractTerm | Enum: month_to_month, twelve_month, twentyfour_month |
| `login` | str \| None | PPPoE username — `font-mono` |
| `ipv4_address` | str \| None | `font-mono` |
| `mac_address` | str \| None | `font-mono` |
| `start_at` | datetime \| None | |
| `end_at` | datetime \| None | |
| `.subscriber` | Subscriber | |
| `.offer` | CatalogOffer | |

### OLT Device

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | |
| `name` | str | |
| `hostname` | str \| None | |
| `vendor` | str \| None | |
| `model` | str \| None | |
| `mgmt_ip` | str \| None | `font-mono` |
| `firmware_version` | str \| None | |
| `status` | DeviceStatus | Enum: active, inactive, maintenance, retired |
| `snmp_enabled` | bool | |
| `.pon_ports` | list[PonPort] | |

### ServiceOrder

| Field | Type | Display Notes |
|-------|------|--------------|
| `id` | UUID | |
| `status` | ServiceOrderStatus | Enum: draft, submitted, scheduled, provisioning, active, canceled, failed |
| `order_type` | ServiceOrderType \| None | Enum: new_install, upgrade, downgrade, disconnect, reconnect, change_service |
| `notes` | str \| None | |
| `.subscriber` | Subscriber | |
| `.subscription` | Subscription \| None | |

---

## HTMX Interaction Map

### Auto-Refresh Partials (Polling)

| Trigger Element | Endpoint | Target | Interval |
|----------------|----------|--------|----------|
| Dashboard stats cards | `GET /admin/dashboard/stats` | `#stats-container` | 30s |
| Activity feed | `GET /admin/dashboard/activity` | `#activity-feed` | 30s |
| Server health widget | `GET /admin/dashboard/server-health` | `#server-health` | 60s |

### Search (Live Filtering)

| Page | Endpoint | Trigger | Target |
|------|----------|---------|--------|
| Customers | `GET /admin/customers?search=...` | `keyup changed delay:300ms` | `#data-table` |
| Subscribers | `GET /admin/subscribers?search=...` | `keyup changed delay:300ms` | `#data-table` |
| Invoices | `GET /admin/billing?search=...` | `keyup changed delay:300ms` | `#data-table` |
| Offers | `GET /admin/catalog/offers?search=...` | `keyup changed delay:300ms` | `#data-table` |

### Dynamic Form Fields

| Trigger | Endpoint | Updates |
|---------|----------|---------|
| Account select (payment form) | `GET /admin/billing/payments/invoice-options?account_id=...` | Invoice dropdown |
| Invoice select (payment form) | `GET /admin/billing/payments/invoice-details?invoice_id=...` | Amount, currency |
| Status filter click (invoices) | `GET /admin/billing?status=...` | Table rows |
| Tab switch (offers) | `GET /admin/catalog/offers?plan_category=...` | Table rows |
| Bulk activate preview | `POST /admin/provisioning/bulk-activate/preview` | Preview table |

### Toast Notifications

Triggered via `HX-Trigger` response header:
```python
# In web service:
response.headers["HX-Trigger"] = json.dumps({
    "showToast": {"message": "Invoice created", "type": "success"}
})
```

Template listens:
```html
<div x-data @show-toast.window="...">
```

---

## Form Patterns

### CSRF Token (Required on every POST form)
```html
<form method="POST" action="/admin/billing/invoices/create">
    {{ csrf_token_field | safe }}
    <!-- fields -->
</form>
```

### POST-Redirect-GET
All form submissions redirect after success:
```
POST /admin/customers/new → 303 → GET /admin/customers/{type}/{id}
POST /admin/billing/invoices/create → 303 → GET /admin/billing
POST /admin/catalog/offers/create → 303 → GET /admin/catalog/offers
```

### Context Pre-fill via Query Parameters
Forms accept URL params to pre-populate:
```
/admin/billing/invoices/new?account_id=xxx     → locks account selector
/admin/billing/payments/new?invoice_id=xxx     → locks invoice, pre-fills amount
/admin/catalog/subscriptions/new?subscriber_id=xxx
```

When pre-filled:
- Show field as read-only with visual indicator
- Include as hidden `<input>` for submission
- Display context banner explaining the pre-fill

### Alpine.js Form State

Complex forms use Alpine.js `x-data` with JSON config from the server:

```html
<!-- CRITICAL: use single quotes for x-data with tojson -->
<div x-data='{{ invoice_config | tojson }}'>
    <input x-model="invoiceNumber" />
    <template x-for="(item, i) in lineItems">
        <!-- dynamic line items -->
    </template>
</div>
```

### Validation Errors
- Re-render form with all values preserved
- Display errors inline next to fields
- Flash message via `HX-Trigger` for general errors

---

## Template Conventions

### None/Empty Handling
```html
{{ value or '' }}
{{ value or 'N/A' }}
{% if value %}{{ value }}{% endif %}
```

### Enum Display (never use `.value` directly)
```python
# In web service context:
STATUS_DISPLAY = {"active": "Active", "new_install": "New Install", ...}
```
```html
{{ STATUS_DISPLAY.get(order.status.value, order.status.value) }}
```

### Dynamic Tailwind Classes (never interpolate)
```python
# In web service context — dict lookup, not f-string:
STATUS_COLORS = {
    "active": "bg-emerald-100 text-emerald-800 dark:bg-emerald-900 dark:text-emerald-200",
    "suspended": "bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200",
}
```

### Data Formatting

| Type | Template Pattern | Example |
|------|-----------------|---------|
| Currency | `{{ "%.2f"\|format(invoice.total) }}` | `5,000.00` |
| Bandwidth | `{{ offer.speed_download_mbps }} Mbps` | `100 Mbps` |
| IP address | `<span class="font-mono">{{ ip }}</span>` | `192.168.1.1` |
| MAC address | `<span class="font-mono">{{ mac }}</span>` | `AA:BB:CC:DD:EE:FF` |
| Date (recent) | Relative: `2 hours ago` | |
| Date (older) | ISO: `2024-01-15` | |
| Percentage | `{{ "%.1f"\|format(value) }}%` | `95.5%` |

### Safe Filter Rules

`| safe` is ONLY allowed for:
- `{{ csrf_token_field | safe }}` — CSRF token
- `{{ data | tojson }}` — JSON for Alpine.js
- Icon macro output — `{{ icon_users() | safe }}` (when passed to other macros)
- Admin-authored CSS

**NEVER** use `| safe` on user-submitted content (XSS risk).

---

## File Structure Reference

```
templates/
├── layouts/
│   ├── admin.html              ← Admin portal base (sidebar + nav)
│   ├── customer.html           ← Customer portal base
│   ├── reseller.html           ← Reseller portal base
│   └── base.html               ← Root base (head, scripts, CSS)
├── admin/
│   ├── dashboard/
│   │   ├── index.html
│   │   ├── _stats.html         ← HTMX partial
│   │   ├── _activity.html      ← HTMX partial
│   │   └── _server_health.html ← HTMX partial
│   ├── customers/
│   │   ├── index.html
│   │   ├── detail.html
│   │   ├── new.html
│   │   └── _table.html         ← HTMX partial
│   ├── subscribers/
│   │   ├── index.html
│   │   └── detail.html
│   ├── billing/
│   │   ├── index.html          ← Invoice list
│   │   ├── invoices/
│   │   │   ├── detail.html
│   │   │   ├── new.html
│   │   │   └── edit.html
│   │   ├── payments/
│   │   │   ├── index.html
│   │   │   ├── new.html
│   │   │   └── detail.html
│   │   └── ...
│   ├── catalog/
│   │   ├── offers/
│   │   │   ├── index.html
│   │   │   ├── new.html
│   │   │   └── detail.html
│   │   └── subscriptions/
│   ├── network/
│   │   ├── olts/
│   │   ├── onts/
│   │   ├── devices/
│   │   └── ...
│   ├── provisioning/
│   ├── reports/
│   ├── system/
│   └── errors/
│       ├── 404.html
│       └── 500.html
├── components/
│   ├── ui/
│   │   └── macros.html         ← 40+ reusable macros
│   └── navigation/
├── auth/
├── customer/
├── reseller/
└── public/
```

---

## Quick Start for Frontend Dev

1. **Read `templates/layouts/admin.html`** — understand the shell
2. **Read `templates/components/ui/macros.html`** — your component library
3. **Pick a module** (e.g., Billing) and read its existing template
4. **Check the context spec above** for what data is available
5. **Use macros** — don't rebuild buttons, tables, badges from scratch
6. **Always pair dark mode** — `bg-white dark:bg-slate-800`
7. **Use module accent colors** — amber for subscribers, emerald for billing, etc.
8. **Test with HTMX** — partials return fragments, not full pages
