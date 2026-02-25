# UI/UX Component Architecture & Page Arrangement

## Macro Import Convention

Every page template imports from one place:

```jinja2
{% from "components/ui/macros.html" import
    ambient_background, page_header, action_button,
    stats_card, status_badge, data_table, table_head, table_row,
    row_actions, row_action, empty_state, card, search_input,
    filter_select, filter_bar, detail_header, tabs, info_row,
    pagination, icon_badge, info_banner, submit_button,
    danger_button, avatar, vendor_badge, type_badge,
    status_filter_card, validated_input
%}
```

Macros are **called** (not included). Components are **included** (not called).
The distinction:

```jinja2
{# Macro — imported, called inline, returns HTML #}
{{ stats_card("Revenue", "$12,500", icon=icon_invoice(), color="emerald") }}

{# Component — included with context, has its own file #}
{% include "components/charts/area_chart.html" %}
```

---

## Page Type Templates

Every page in the app falls into one of 6 page types. Each has a standardized arrangement.

---

### Page Type 1: MODULE DASHBOARD

**Used by:** Main Dashboard, Billing, Catalog, Provisioning, Network, Notifications, Integrations, RADIUS, Usage, Collections, VPN, GIS, System, Resellers

**Layout:**

```
┌─────────────────────────────────────────────────────────┐
│ ambient_background(module_color, module_color2)         │
├─────────────────────────────────────────────────────────┤
│ page_header(title, subtitle, icon, color)               │
│   └─ action_button() slots (top-right)                  │
├─────────────────────────────────────────────────────────┤
│ KPI CARDS ROW  (grid 1 → 2 → 4)                        │
│ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐    │
│ │stats_card│ │stats_card│ │stats_card│ │stats_card│    │
│ └──────────┘ └──────────┘ └──────────┘ └──────────┘    │
├─────────────────────────────────────────────────────────┤
│ CHARTS ROW  (grid 1 → 2)                               │
│ ┌────────────────────────┐ ┌────────────────────────┐   │
│ │ card()                 │ │ card()                 │   │
│ │  └─ area_chart / line  │ │  └─ doughnut / bar     │   │
│ └────────────────────────┘ └────────────────────────┘   │
├─────────────────────────────────────────────────────────┤
│ ATTENTION / TABLES ROW  (grid 1 → 3, span 2+1)         │
│ ┌─────────────────────────────┐ ┌──────────────────┐    │
│ │ data_table() — recent items │ │ card() — alerts  │    │
│ │  └─ table_head + table_row  │ │  or quick actions│    │
│ └─────────────────────────────┘ └──────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

**Template skeleton:**

```jinja2
{% extends "layouts/admin.html" %}
{% from "components/ui/macros.html" import
    ambient_background, page_header, stats_card, card,
    data_table, table_head, table_row, row_actions, row_action,
    action_button, status_badge, empty_state,
    icon_invoice, icon_users, icon_server, icon_check
%}

{% block charts_js %}
<script src="/static/js/charts.js"></script>
{% endblock %}

{% block content %}
{{ ambient_background(color, color2) }}

{% call page_header(
    title="Module Name",
    subtitle="Brief description of this module",
    icon=icon_module(),
    color=color, color2=color2
) %}
    {{ action_button("Primary Action", href, icon_plus(), "primary", color, color2) }}
{% endcall %}

{# ── KPI Cards ────────────────────────────────── #}
<div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-6"
     style="animation-delay: 0.1s">
    {{ stats_card("Label 1", stats.value1, icon=icon_1(), color="emerald",
                   change=stats.trend1, change_type="increase", href="/admin/...") }}
    {{ stats_card("Label 2", stats.value2, icon=icon_2(), color="blue",
                   change=stats.trend2, change_type="increase") }}
    {{ stats_card("Label 3", stats.value3, icon=icon_3(), color="amber") }}
    {{ stats_card("Label 4", stats.value4, icon=icon_4(), color="rose") }}
</div>

{# ── Charts Row ───────────────────────────────── #}
<div class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6"
     style="animation-delay: 0.15s">
    {% call card(title="Trend", icon=icon_chart(), color=color) %}
        <canvas id="trend-chart" height="280"></canvas>
    {% endcall %}

    {% call card(title="Distribution", icon=icon_chart(), color=color) %}
        <canvas id="distribution-chart" height="280"></canvas>
    {% endcall %}
</div>

{# ── Tables / Actions Row ─────────────────────── #}
<div class="grid grid-cols-1 lg:grid-cols-3 gap-6"
     style="animation-delay: 0.2s">
    <div class="lg:col-span-2">
        {% call data_table(title="Recent Items", icon=icon_list(), color=color, count=items|length) %}
            <table class="min-w-full divide-y divide-slate-200 dark:divide-slate-700">
                {{ table_head([
                    {"label": "Name", "width": "40%"},
                    {"label": "Status", "width": "20%"},
                    {"label": "Date", "width": "25%"},
                    {"label": "", "width": "15%", "align": "right"},
                ]) }}
                <tbody class="divide-y divide-slate-200 dark:divide-slate-700">
                {% for item in items[:5] %}
                    {% call table_row() %}
                        <td>{{ item.name }}</td>
                        <td>{{ status_badge(item.status) }}</td>
                        <td>{{ item.created_at.strftime('%Y-%m-%d') }}</td>
                        <td>{% call row_actions() %}
                            {{ row_action(href="/admin/.../"|string + item.id, icon=icon_view(), title="View") }}
                        {% endcall %}</td>
                    {% endcall %}
                {% else %}
                    {{ empty_state("No items yet", colspan=4) }}
                {% endfor %}
                </tbody>
            </table>
        {% endcall %}
    </div>

    <div>
        {% call card(title="Quick Actions", icon=icon_check(), color=color) %}
            {# Action buttons grid #}
        {% endcall %}
    </div>
</div>
{% endblock %}

{% block scripts %}
<script>
document.addEventListener('DOMContentLoaded', () => {
    DotmacCharts.createAreaChart(
        document.getElementById('trend-chart').getContext('2d'),
        {{ chart_data.trend | tojson }},
        {}
    );
    DotmacCharts.createDoughnutChart(
        document.getElementById('distribution-chart').getContext('2d'),
        {{ chart_data.distribution | tojson }},
        {}
    );
});
</script>
{% endblock %}
```

---

### Page Type 2: LIST PAGE

**Used by:** Subscriber list, Invoice list, Payment list, Offer list, Order list, Device list, every CRUD table

**Layout:**

```
┌─────────────────────────────────────────────────────────┐
│ ambient_background()                                    │
├─────────────────────────────────────────────────────────┤
│ page_header(title, subtitle, icon)                      │
│   └─ action_button("Create ...", href)                  │
├─────────────────────────────────────────────────────────┤
│ STATUS FILTER CARDS (optional, grid 1 → 2 → 4)         │
│ ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────┐│
│ │status_card │ │status_card │ │status_card │ │stat_cd ││
│ │ All (120)  │ │ Active(95) │ │Pending(15) │ │Err(10) ││
│ └────────────┘ └────────────┘ └────────────┘ └────────┘│
├─────────────────────────────────────────────────────────┤
│ FILTER BAR                                              │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ filter_bar()                                        │ │
│ │  ├─ search_input(hx-get, hx-target="#table-body")   │ │
│ │  ├─ filter_select("status", options, hx-get)        │ │
│ │  ├─ filter_select("type", options, hx-get)          │ │
│ │  └─ Clear filters link                              │ │
│ └─────────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────┤
│ DATA TABLE (full width)                                 │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ data_table(title, icon, count)                      │ │
│ │  ├─ table_head([columns])                           │ │
│ │  ├─ table_row() × N                                 │ │
│ │  │   ├─ checkbox (bulk select)                      │ │
│ │  │   ├─ data cells                                  │ │
│ │  │   ├─ status_badge()                              │ │
│ │  │   └─ row_actions()                               │ │
│ │  │       ├─ row_action(view)                        │ │
│ │  │       ├─ row_action(edit)                        │ │
│ │  │       └─ row_action(delete)                      │ │
│ │  └─ empty_state() (when no results)                 │ │
│ ├─────────────────────────────────────────────────────┤ │
│ │ pagination(page, total_pages, total, per_page, url) │ │
│ └─────────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────┤
│ BULK ACTIONS BAR (fixed bottom, shown when selected)    │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ "X selected" │ Action 1 │ Action 2 │ Clear          │ │
│ └─────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

**Table column arrangements by entity type:**

```
Subscribers:  □ | Avatar+Name | Account# | Plan | Status | Balance | Last Payment | Actions
Invoices:     □ | Invoice#    | Customer | Amount | Balance | Status | Due Date    | Actions
Payments:     □ | Payment#    | Customer | Amount | Channel | Status | Date        | Actions
Offers:       □ | Name        | Type     | Speed  | Price   | Subs   | Status      | Actions
Orders:       □ | Order#      | Customer | Type   | Priority| Status | Scheduled   | Actions
Devices:      □ | Name        | Type     | IP     | Model   | Status | Uptime      | Actions
```

**Column alignment rules:**
- Text: `text-left` (names, descriptions)
- Numbers/currency: `text-right font-mono tabular-nums`
- Status badges: `text-center`
- Dates: `text-left` (or `text-right` for timestamps)
- Actions: `text-right`
- Checkboxes: `text-center w-12`

**HTMX partial pattern:**

The table body is extracted to a `_table.html` partial for HTMX swap:

```jinja2
{# Main page: includes partial on first load #}
<div id="table-container">
    {% include "admin/module/_table.html" %}
</div>

{# Filters target: hx-target="#table-container" #}
```

```jinja2
{# _table.html partial: returned by HTMX filter/sort/paginate requests #}
{% from "components/ui/macros.html" import ... %}

{% call data_table(...) %}
    <table>
        {{ table_head(columns) }}
        <tbody>
        {% for item in items %}
            {% call table_row() %}...{% endcall %}
        {% endfor %}
        </tbody>
    </table>
{% endcall %}
{{ pagination(...) }}
```

---

### Page Type 3: DETAIL PAGE

**Used by:** Subscriber detail, Invoice detail, Payment detail, Offer detail, Order detail, Device detail, NAS detail

**Layout:**

```
┌─────────────────────────────────────────────────────────┐
│ ambient_background()                                    │
├─────────────────────────────────────────────────────────┤
│ detail_header(title, subtitle, icon, back_href, status) │
│   └─ action_button() slots (edit, delete, etc.)         │
├─────────────────────────────────────────────────────────┤
│ TABS (optional)                                         │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ tabs(items=[{label, value, href, count}], active)   │ │
│ └─────────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────┤
│ CONTENT (grid 1 → 3, main + sidebar)                   │
│ ┌────────────────────────────────┐ ┌──────────────────┐ │
│ │ LEFT COLUMN (lg:col-span-2)   │ │ RIGHT SIDEBAR    │ │
│ │                                │ │ (lg:col-span-1)  │ │
│ │ card(title="Section 1")       │ │ (sticky top-6)   │ │
│ │  ├─ info_row("Label", value)  │ │                  │ │
│ │  ├─ info_row("Label", value)  │ │ card("Summary")  │ │
│ │  └─ info_row("Label", value)  │ │  ├─ key stats    │ │
│ │                                │ │  └─ balance      │ │
│ │ card(title="Section 2")       │ │                  │ │
│ │  └─ data_table (sub-items)    │ │ card("Actions")  │ │
│ │                                │ │  ├─ button 1     │ │
│ │ card(title="Activity")        │ │  ├─ button 2     │ │
│ │  └─ activity timeline         │ │  └─ button 3     │ │
│ └────────────────────────────────┘ └──────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

**Template skeleton:**

```jinja2
{% extends "layouts/admin.html" %}
{% from "components/ui/macros.html" import
    ambient_background, detail_header, action_button, card, info_row,
    tabs, status_badge, data_table, table_head, table_row,
    row_actions, row_action, danger_button, icon_edit, icon_delete, icon_view
%}

{% block content %}
{{ ambient_background(color, color2) }}

{% call detail_header(
    title=item.name,
    subtitle="ID: " ~ item.id[:8],
    icon=icon_module(),
    color=color, color2=color2,
    back_href="/admin/module",
    back_label="Back to List",
    status=item.status
) %}
    {{ action_button("Edit", "/admin/module/" ~ item.id ~ "/edit", icon_edit(), "secondary") }}
    {{ danger_button("Delete", confirm_title="Delete Item?",
                     confirm_message="This cannot be undone.",
                     action_url="/admin/module/" ~ item.id ~ "/delete") }}
{% endcall %}

{# ── Tabs ─────────────────────────────────────── #}
{{ tabs(
    items=[
        {"label": "Overview", "value": "overview", "href": "#overview"},
        {"label": "Services", "value": "services", "href": "#services", "count": services|length},
        {"label": "Billing",  "value": "billing",  "href": "#billing",  "count": invoices|length},
        {"label": "Activity", "value": "activity",  "href": "#activity"},
    ],
    active=active_tab or "overview",
    color=color
) }}

{# ── Content Grid ─────────────────────────────── #}
<div class="grid grid-cols-1 lg:grid-cols-3 gap-6 mt-6">

    {# ── Left Column ──────────────────────────── #}
    <div class="lg:col-span-2 space-y-6">

        {% call card(title="Details", icon=icon_info(), color=color) %}
            <div class="divide-y divide-slate-100 dark:divide-slate-700">
                {{ info_row("Name", item.name) }}
                {{ info_row("Email", item.email) }}
                {% call info_row("Status") %}
                    {{ status_badge(item.status) }}
                {% endcall %}
                {{ info_row("Created", item.created_at.strftime('%Y-%m-%d %H:%M')) }}
            </div>
        {% endcall %}

        {% call card(title="Related Items", icon=icon_list(), color=color) %}
            {% call data_table() %}
                {# Sub-table of related entities #}
            {% endcall %}
        {% endcall %}

    </div>

    {# ── Right Sidebar ────────────────────────── #}
    <div class="space-y-6 lg:sticky lg:top-6 self-start">

        {% call card(title="Summary", icon=icon_chart(), color=color) %}
            <div class="space-y-3">
                <div class="flex justify-between">
                    <span class="text-sm text-slate-500">Balance</span>
                    <span class="text-sm font-mono font-semibold">{{ item.balance | format_currency }}</span>
                </div>
            </div>
        {% endcall %}

        {% call card(title="Quick Actions", icon=icon_check(), color=color) %}
            <div class="space-y-2">
                {{ action_button("Create Invoice", href, icon_plus(), "secondary", color, size="sm") }}
                {{ action_button("Create Order", href, icon_plus(), "secondary", color, size="sm") }}
            </div>
        {% endcall %}

    </div>
</div>
{% endblock %}
```

**info_row arrangements for different entity types:**

```
Subscriber Detail:
  ├─ Name          │ Account #     (2-column info grid)
  ├─ Email         │ Phone
  ├─ Type          │ Status
  ├─ Organization  │ Reseller
  ├─ Address       │ Region
  └─ Created       │ Updated

Invoice Detail:
  ├─ Invoice #     │ Status
  ├─ Customer      │ Account
  ├─ Issue Date    │ Due Date
  ├─ Currency      │ Payment Terms
  └─ Subtotal → Tax → Total (summary card)

Device Detail:
  ├─ Name          │ Type
  ├─ IP Address    │ MAC Address       (font-mono)
  ├─ Vendor        │ Model
  ├─ Firmware      │ Serial
  ├─ Site          │ Location
  └─ Status        │ Uptime
```

**Two-column info grid macro (NEW — to add):**

```jinja2
{% macro info_grid() %}
<div class="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-1 divide-y divide-slate-100
            dark:divide-slate-700 sm:divide-y-0">
    {{ caller() }}
</div>
{% endmacro %}
```

---

### Page Type 4: FORM PAGE

**Used by:** Subscriber create/edit, Invoice create/edit, Offer create/edit, Order create, all settings forms

**Layout:**

```
┌─────────────────────────────────────────────────────────┐
│ page_header(title="Create/Edit X", back_href)           │
├─────────────────────────────────────────────────────────┤
│ ERROR ALERT (if validation errors)                      │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ alert(type="error", message=error_message)          │ │
│ └─────────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────┤
│ FORM (grid 1 → 3, main + sidebar)                      │
│ ┌────────────────────────────────┐ ┌──────────────────┐ │
│ │ LEFT COLUMN (lg:col-span-2)   │ │ RIGHT SIDEBAR    │ │
│ │                                │ │ (lg:col-span-1)  │ │
│ │ card("Basic Information")     │ │ (sticky top-6)   │ │
│ │  ├─ form field (full width)   │ │                  │ │
│ │  ├─ 2-col: field │ field      │ │ card("Summary")  │ │
│ │  └─ 3-col: field │ fld │ fld  │ │  └─ live totals  │ │
│ │                                │ │                  │ │
│ │ card("Configuration")         │ │ card("Options")  │ │
│ │  ├─ field                      │ │  ├─ checkbox     │ │
│ │  └─ field                      │ │  └─ checkbox     │ │
│ │                                │ │                  │ │
│ │ card("Line Items")  (if appl.)│ │ card("Actions")  │ │
│ │  └─ repeatable_group()        │ │  ├─ submit_btn   │ │
│ │                                │ │  └─ cancel_btn   │ │
│ └────────────────────────────────┘ └──────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

**Form field arrangement rules:**

```
Single field (full width):
  ┌──────────────────────────────────────────────┐
  │ label                                         │
  │ ┌──────────────────────────────────────────┐  │
  │ │ input / textarea / select                │  │
  │ └──────────────────────────────────────────┘  │
  │ helper text                                   │
  └──────────────────────────────────────────────┘

Two fields side by side:
  <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
  ┌─────────────────────┐ ┌─────────────────────┐
  │ label               │ │ label               │
  │ ┌─────────────────┐ │ │ ┌─────────────────┐ │
  │ │ input           │ │ │ │ input           │ │
  │ └─────────────────┘ │ │ └─────────────────┘ │
  └─────────────────────┘ └─────────────────────┘

Three fields:
  <div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
  │ input        │ │ input        │ │ input        │
  └──────────────┘ └──────────────┘ └──────────────┘
```

**Form section ordering per entity:**

```
Subscriber Form:
  Section 1: "Personal Information"
    ├─ 2-col: First Name │ Last Name
    ├─ full:  Email
    ├─ 2-col: Phone │ Alt Phone
    └─ full:  Type (select: individual/organization)

  Section 2: "Address"
    ├─ full:  Street Address
    ├─ 3-col: City │ State │ Postal Code
    └─ full:  Country (select)

  Section 3: "Service"
    ├─ full:  Plan (select from catalog offers)
    └─ 2-col: Reseller │ Region Zone

  Sidebar:
    ├─ Status (select)
    └─ Submit / Cancel

Invoice Form:
  Section 1: "Invoice Details"
    ├─ full:  Customer (typeahead select)
    ├─ 2-col: Invoice # │ Status
    ├─ 3-col: Currency │ Issue Date │ Due Date
    └─ full:  Memo (textarea)

  Section 2: "Line Items"
    └─ repeatable_group("items")
        per row: Description(5) │ Qty(2) │ Price(2) │ Tax(2) │ Total(1)

  Sidebar (sticky):
    ├─ Summary Card
    │   ├─ Line items count
    │   ├─ Subtotal
    │   ├─ Tax
    │   └─ Total (large, bold)
    ├─ Options Card
    │   ├─ Issue immediately (checkbox)
    │   └─ Send email (checkbox)
    └─ Actions Card
        ├─ submit_button("Save Invoice")
        └─ Cancel link

Offer Form:
  Section 1: "Plan Details"
    ├─ full:  Name
    ├─ 2-col: Service Type │ Status
    └─ full:  Description (textarea)

  Section 2: "Pricing"
    ├─ 3-col: Price │ Setup Fee │ Billing Cycle
    └─ 2-col: Currency │ Tax Rate

  Section 3: "Specifications"
    ├─ 2-col: Download Speed │ Upload Speed
    ├─ 2-col: Data Cap │ FUP Threshold
    └─ full:  Contention Ratio

  Section 4: "RADIUS Profile"
    └─ full:  Profile (select)

  Sidebar:
    └─ Submit / Cancel
```

**Using form component macros:**

```jinja2
{% call card(title="Personal Information", icon=icon_user(), color="indigo") %}
<div class="space-y-4">
    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
        {% include "components/forms/input.html" with context %}
        {# OR use inline HTML matching the pattern: #}
        <div>
            <label for="first_name"
                   class="block text-sm font-medium text-slate-700 dark:text-slate-300 mb-1">
                First Name <span class="text-rose-500">*</span>
            </label>
            <input type="text" id="first_name" name="first_name"
                   value="{{ form.first_name or '' }}"
                   class="w-full rounded-lg border border-slate-300 dark:border-slate-600
                          bg-white dark:bg-slate-700 px-3 py-2 text-sm
                          text-slate-900 dark:text-white
                          focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500
                          placeholder:text-slate-400"
                   placeholder="Enter first name" required>
            {% if errors.first_name %}
            <p class="mt-1 text-xs text-rose-500">{{ errors.first_name }}</p>
            {% endif %}
        </div>
    </div>
</div>
{% endcall %}
```

---

### Page Type 5: SETTINGS PAGE

**Used by:** Domain settings, RADIUS config, Billing config, Notification config, all integration settings

**Layout:**

```
┌─────────────────────────────────────────────────────────┐
│ page_header("Settings: Domain Name")                    │
├─────────────────────────────────────────────────────────┤
│ SETTINGS NAVIGATION (left) + CONTENT (right)            │
│ ┌──────────────┐ ┌──────────────────────────────────┐   │
│ │ Nav Links    │ │ card("Section 1")                │   │
│ │              │ │  ├─ setting toggle               │   │
│ │ > Section 1  │ │  ├─ setting input                │   │
│ │   Section 2  │ │  └─ setting select               │   │
│ │   Section 3  │ │                                  │   │
│ │   Section 4  │ │ card("Section 2")                │   │
│ │              │ │  ├─ setting input                │   │
│ │              │ │  ├─ setting input (masked)       │   │
│ │              │ │  └─ Test Connection btn          │   │
│ │              │ │                                  │   │
│ │              │ │ ┌─────────────────────────────┐  │   │
│ │              │ │ │ Save │ Reset to Defaults     │  │   │
│ │              │ │ └─────────────────────────────┘  │   │
│ └──────────────┘ └──────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

**Alternative: full-width settings (no left nav, for simpler domains):**

```
┌─────────────────────────────────────────────────────────┐
│ page_header("Settings: Domain Name")                    │
├─────────────────────────────────────────────────────────┤
│ card("Section 1")                                       │
│  ├─ setting row: label │ input │ source indicator       │
│  ├─ setting row: label │ toggle │ default indicator     │
│  └─ setting row: label │ select │ help text             │
│                                                         │
│ card("Section 2")                                       │
│  └─ ...                                                 │
│                                                         │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ Save Changes │ Reset │ Test                          │ │
│ └─────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

**Setting row macro (NEW — to add):**

```jinja2
{% macro setting_row(label, description="", source="default", is_secret=False) %}
<div class="flex items-start justify-between py-4
            border-b border-slate-100 dark:border-slate-700 last:border-0">
    <div class="flex-1 mr-6">
        <label class="text-sm font-medium text-slate-700 dark:text-slate-300">
            {{ label }}
        </label>
        {% if description %}
        <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">{{ description }}</p>
        {% endif %}
        {% if source != "default" %}
        <span class="inline-flex items-center text-xs text-slate-400 mt-1">
            {% if source == "env" %}
                <svg class="w-3 h-3 mr-1">...</svg> From environment
            {% elif source == "db" %}
                <svg class="w-3 h-3 mr-1">...</svg> Custom value
            {% endif %}
        </span>
        {% endif %}
    </div>
    <div class="flex-shrink-0 w-64">
        {{ caller() }}
    </div>
</div>
{% endmacro %}

{% macro setting_toggle(name, value, label="", description="") %}
<label class="relative inline-flex items-center cursor-pointer">
    <input type="checkbox" name="{{ name }}" value="true"
           class="sr-only peer" {{ "checked" if value }}>
    <div class="w-11 h-6 bg-slate-200 peer-focus:ring-2 peer-focus:ring-indigo-500
                rounded-full peer dark:bg-slate-600
                peer-checked:bg-indigo-600 peer-checked:after:translate-x-full
                after:content-[''] after:absolute after:top-[2px] after:start-[2px]
                after:bg-white after:rounded-full after:h-5 after:w-5
                after:transition-all"></div>
</label>
{% endmacro %}
```

---

### Page Type 6: WIZARD / MULTI-STEP FORM

**Used by:** Subscriber onboarding, Import wizard, Payment arrangement setup

**Layout:**

```
┌─────────────────────────────────────────────────────────┐
│ page_header("Create Subscriber")                        │
├─────────────────────────────────────────────────────────┤
│ STEP INDICATOR                                          │
│ ┌─────────────────────────────────────────────────────┐ │
│ │  ●──────●──────○──────○──────○                      │ │
│ │ Type  Info  Address Contact Review                  │ │
│ └─────────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────┤
│ STEP CONTENT (centered, max-w-2xl)                      │
│ ┌─────────────────────────────────────────────────────┐ │
│ │ card("Step Title")                                  │ │
│ │  └─ form fields for this step                       │ │
│ └─────────────────────────────────────────────────────┘ │
├─────────────────────────────────────────────────────────┤
│ NAVIGATION                                              │
│ ┌─────────────────────────────────────────────────────┐ │
│ │          ← Previous              Next →             │ │
│ └─────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

**Step indicator macro (NEW — to add):**

```jinja2
{% macro step_indicator(steps, current, color="indigo") %}
<nav class="flex items-center justify-center mb-8">
    {% for step in steps %}
    <div class="flex items-center">
        {# Step circle #}
        <div class="flex items-center justify-center w-8 h-8 rounded-full text-sm font-medium
            {% if loop.index < current %}
                bg-{{ color }}-600 text-white
            {% elif loop.index == current %}
                bg-{{ color }}-600 text-white ring-4 ring-{{ color }}-100 dark:ring-{{ color }}-900/30
            {% else %}
                bg-slate-200 dark:bg-slate-700 text-slate-500 dark:text-slate-400
            {% endif %}">
            {% if loop.index < current %}
                <svg class="w-4 h-4">...</svg>
            {% else %}
                {{ loop.index }}
            {% endif %}
        </div>
        {# Step label #}
        <span class="ml-2 text-sm
            {% if loop.index <= current %}text-slate-900 dark:text-white font-medium
            {% else %}text-slate-500 dark:text-slate-400{% endif %}">
            {{ step }}
        </span>
        {# Connector line #}
        {% if not loop.last %}
        <div class="w-12 h-0.5 mx-3
            {% if loop.index < current %}bg-{{ color }}-600
            {% else %}bg-slate-200 dark:bg-slate-700{% endif %}">
        </div>
        {% endif %}
    </div>
    {% endfor %}
</nav>
{% endmacro %}
```

---

## New Macros to Add

Based on the patterns above, these macros should be added to `components/ui/macros.html`:

### 1. `info_grid` — Two-column key-value layout for detail pages

```jinja2
{% macro info_grid() %}
<div class="grid grid-cols-1 sm:grid-cols-2 gap-x-8">
    {{ caller() }}
</div>
{% endmacro %}
```

### 2. `setting_row` — Settings page key-value-input row

```jinja2
{% macro setting_row(label, description="", source="default") %}
<div class="flex items-start justify-between py-4
            border-b border-slate-100 dark:border-slate-700 last:border-0">
    <div class="flex-1 mr-6">
        <label class="text-sm font-medium text-slate-700 dark:text-slate-300">{{ label }}</label>
        {% if description %}
        <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">{{ description }}</p>
        {% endif %}
        {% if source == "env" %}
        <span class="inline-flex items-center text-xs text-blue-500 mt-1">
            From environment variable
        </span>
        {% endif %}
    </div>
    <div class="flex-shrink-0 w-72">{{ caller() }}</div>
</div>
{% endmacro %}
```

### 3. `setting_toggle` — Boolean setting switch

```jinja2
{% macro setting_toggle(name, value=False) %}
<label class="relative inline-flex items-center cursor-pointer">
    <input type="checkbox" name="{{ name }}" value="true" class="sr-only peer"
           {{ "checked" if value }}>
    <div class="w-11 h-6 bg-slate-200 rounded-full peer dark:bg-slate-600
                peer-focus:ring-2 peer-focus:ring-indigo-500
                peer-checked:bg-indigo-600 peer-checked:after:translate-x-full
                after:content-[''] after:absolute after:top-[2px] after:start-[2px]
                after:bg-white after:rounded-full after:h-5 after:w-5
                after:transition-all"></div>
</label>
{% endmacro %}
```

### 4. `step_indicator` — Multi-step wizard progress

```jinja2
{% macro step_indicator(steps, current, color="indigo") %}
{# See wizard section above for full implementation #}
{% endmacro %}
```

### 5. `metric_row` — Compact metric for sidebar summary cards

```jinja2
{% macro metric_row(label, value, color="slate", mono=False) %}
<div class="flex items-center justify-between py-2">
    <span class="text-sm text-slate-500 dark:text-slate-400">{{ label }}</span>
    <span class="text-sm font-semibold text-{{ color }}-600 dark:text-{{ color }}-400
                 {{ 'font-mono tabular-nums' if mono }}">{{ value }}</span>
</div>
{% endmacro %}
```

### 6. `section_divider` — Visual separator between form sections

```jinja2
{% macro section_divider(label="") %}
{% if label %}
<div class="relative my-6">
    <div class="absolute inset-0 flex items-center"><div class="w-full border-t border-slate-200 dark:border-slate-700"></div></div>
    <div class="relative flex justify-start">
        <span class="bg-white dark:bg-slate-800 pr-3 text-sm font-medium text-slate-500">{{ label }}</span>
    </div>
</div>
{% else %}
<hr class="my-6 border-slate-200 dark:border-slate-700">
{% endif %}
{% endmacro %}
```

### 7. `alert_count_badge` — Attention badge for sidebar/nav items

```jinja2
{% macro alert_count_badge(count, color="rose") %}
{% if count and count > 0 %}
<span class="inline-flex items-center justify-center min-w-[1.25rem] h-5 px-1.5
             rounded-full text-xs font-medium
             bg-{{ color }}-100 text-{{ color }}-700
             dark:bg-{{ color }}-900 dark:text-{{ color }}-300">
    {{ count if count < 100 else "99+" }}
</span>
{% endif %}
{% endmacro %}
```

### 8. `connection_status` — Live connection indicator for devices/integrations

```jinja2
{% macro connection_status(status, label="", last_seen="") %}
<div class="flex items-center gap-2">
    <span class="relative flex h-2.5 w-2.5">
        {% if status == "online" %}
        <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
        <span class="relative inline-flex rounded-full h-2.5 w-2.5 bg-emerald-500"></span>
        {% elif status == "degraded" %}
        <span class="relative inline-flex rounded-full h-2.5 w-2.5 bg-amber-500"></span>
        {% else %}
        <span class="relative inline-flex rounded-full h-2.5 w-2.5 bg-rose-500"></span>
        {% endif %}
    </span>
    {% if label %}
    <span class="text-sm text-slate-700 dark:text-slate-300">{{ label }}</span>
    {% endif %}
    {% if last_seen %}
    <span class="text-xs text-slate-400">{{ last_seen }}</span>
    {% endif %}
</div>
{% endmacro %}
```

### 9. `progress_bar` — Utilization bar for IP pools, quotas, disk

```jinja2
{% macro progress_bar(value, max=100, label="", show_text=True, size="md", color="auto") %}
{% set pct = ((value / max * 100) if max > 0 else 0) | round(1) %}
{% if color == "auto" %}
    {% set bar_color = "emerald" if pct < 70 else ("amber" if pct < 85 else "rose") %}
{% else %}
    {% set bar_color = color %}
{% endif %}
<div>
    {% if label or show_text %}
    <div class="flex justify-between mb-1">
        {% if label %}<span class="text-xs text-slate-500 dark:text-slate-400">{{ label }}</span>{% endif %}
        {% if show_text %}<span class="text-xs font-mono text-slate-600 dark:text-slate-300">{{ value }}/{{ max }} ({{ pct }}%)</span>{% endif %}
    </div>
    {% endif %}
    <div class="w-full bg-slate-200 dark:bg-slate-700 rounded-full
                {{ 'h-1.5' if size == 'sm' else ('h-2.5' if size == 'md' else 'h-4') }}">
        <div class="bg-{{ bar_color }}-500 rounded-full transition-all duration-500
                    {{ 'h-1.5' if size == 'sm' else ('h-2.5' if size == 'md' else 'h-4') }}"
             style="width: {{ pct }}%"></div>
    </div>
</div>
{% endmacro %}
```

### 10. `timeline_item` — Activity/audit timeline entry

```jinja2
{% macro timeline_item(title, description="", time="", icon="", color="slate", is_last=False) %}
<div class="relative pl-8 pb-6 {{ '' if is_last else 'border-l-2 border-slate-200 dark:border-slate-700' }}">
    <div class="absolute -left-2 top-0 flex items-center justify-center w-4 h-4 rounded-full
                bg-{{ color }}-100 dark:bg-{{ color }}-900 ring-4 ring-white dark:ring-slate-800">
        <div class="w-1.5 h-1.5 rounded-full bg-{{ color }}-500"></div>
    </div>
    <div class="flex items-start justify-between">
        <div>
            <p class="text-sm font-medium text-slate-900 dark:text-white">{{ title }}</p>
            {% if description %}
            <p class="text-xs text-slate-500 dark:text-slate-400 mt-0.5">{{ description }}</p>
            {% endif %}
        </div>
        {% if time %}
        <span class="text-xs text-slate-400 whitespace-nowrap ml-4">{{ time }}</span>
        {% endif %}
    </div>
</div>
{% endmacro %}
```

---

## Macro Organization

All macros live in `components/ui/macros.html`, organized by category:

```
components/ui/macros.html
│
├── LAYOUT
│   ├── ambient_background()
│   ├── page_header()
│   ├── detail_header()
│   ├── section_divider()          ← NEW
│   └── info_grid()                ← NEW
│
├── DATA DISPLAY
│   ├── stats_card()
│   ├── status_badge()
│   ├── type_badge()
│   ├── vendor_badge()
│   ├── avatar()
│   ├── icon_badge()
│   ├── info_row()
│   ├── metric_row()               ← NEW
│   ├── progress_bar()             ← NEW
│   ├── connection_status()        ← NEW
│   ├── alert_count_badge()        ← NEW
│   └── timeline_item()            ← NEW
│
├── TABLES
│   ├── data_table()
│   ├── table_head()
│   ├── table_row()
│   ├── row_actions()
│   ├── row_action()
│   └── empty_state()
│
├── NAVIGATION
│   ├── tabs()
│   ├── pagination()
│   ├── status_filter_card()
│   └── step_indicator()           ← NEW
│
├── CONTAINERS
│   ├── card()
│   ├── filter_bar()
│   └── info_banner()
│
├── INPUTS
│   ├── search_input()
│   ├── filter_select()
│   ├── validated_input()
│   ├── submit_button()
│   ├── danger_button()
│   └── warning_button()
│
├── SETTINGS                        ← NEW SECTION
│   ├── setting_row()
│   └── setting_toggle()
│
├── BUTTONS
│   └── action_button()
│
└── ICONS
    ├── icon_plus()
    ├── icon_view()
    ├── icon_edit()
    ├── icon_delete()
    └── ... (15+ icon macros)
```

---

## Standalone Components (included, not imported)

These live as separate files and are included with template context:

```
components/
├── charts/
│   ├── area_chart.html          context: chart_id, chart_data, height
│   ├── bar_chart.html           context: chart_id, chart_data, height, horizontal, stacked
│   ├── doughnut_chart.html      context: chart_id, chart_data, height, pie
│   ├── line_chart.html          context: chart_id, chart_data, height
│   └── sparkline.html           context: chart_id, data, color, width, height
│
├── forms/
│   ├── input.html               context: name, label, type, value, error, required
│   ├── select.html              context: name, label, options, value, error
│   ├── textarea.html            context: name, label, value, rows, error
│   ├── csrf_input.html          context: (uses request.state.csrf_token)
│   └── repeatable_group.html    macro: repeatable_group(), line_item_row()
│
├── feedback/
│   ├── alert.html               context: type, title, message, dismissible
│   ├── toast_container.html     (global, in layout)
│   ├── skeleton.html            context: variant, lines
│   └── loading.html             context: size, text
│
├── modals/
│   ├── confirm_modal.html       (global, in layout — triggered via Alpine events)
│   └── modal.html               context: name, title, size (triggered via events)
│
├── buttons/
│   └── button.html              context: variant, label, icon, href, hx_*
│
├── data/
│   ├── stats_card.html          context: title, value, icon, change, href
│   ├── table_interactive.html   context: table_id, columns, data_url
│   ├── table_pagination.html    context: total, limit, offset, htmx_url
│   ├── empty_state.html         context: icon, title, message, action_url
│   ├── recent_activity_panel.html context: activities
│   └── card.html                context: title, subtitle (blocks: card_content, card_footer)
│
├── navigation/
│   ├── admin_sidebar.html       context: active_page, active_menu
│   └── dropdown.html
│
└── _file_upload.html            macro: file_upload_zone()
    _import_wizard.html          context: entity_name, columns, preview_url
```

---

## Chart Data Format

All charts use `DotmacCharts` wrapper over Chart.js. Data format passed from service:

```python
# Area/Line chart data (from service)
revenue_chart = {
    "labels": ["Jan", "Feb", "Mar", ...],
    "datasets": [
        {
            "label": "Billed",
            "data": [12000, 15000, 13000, ...],
            "borderColor": "#10b981",
            "backgroundColor": "rgba(16, 185, 129, 0.1)",
        },
        {
            "label": "Collected",
            "data": [11000, 14000, 12500, ...],
            "borderColor": "#6366f1",
            "backgroundColor": "rgba(99, 102, 241, 0.1)",
        },
    ]
}

# Doughnut chart data
status_chart = {
    "labels": ["Active", "Suspended", "Canceled"],
    "datasets": [{
        "data": [95, 15, 10],
        "backgroundColor": ["#10b981", "#f59e0b", "#f43f5e"],
    }]
}

# Bar chart data
plan_chart = {
    "labels": ["Basic 10M", "Standard 50M", "Premium 100M"],
    "datasets": [{
        "label": "Subscribers",
        "data": [45, 80, 35],
        "backgroundColor": "#8b5cf6",
    }]
}
```

**Template usage:**

```jinja2
{% call card(title="Revenue Trend", icon=icon_chart(), color="emerald") %}
    <div class="flex items-center gap-2 mb-4">
        <select id="trend-period" class="text-xs rounded-lg border-slate-300 ...">
            <option value="6">Last 6 months</option>
            <option value="12">Last 12 months</option>
        </select>
    </div>
    <canvas id="revenue-chart" height="280"></canvas>
{% endcall %}

{% block scripts %}
<script>
document.addEventListener('DOMContentLoaded', () => {
    DotmacCharts.createAreaChart(
        document.getElementById('revenue-chart').getContext('2d'),
        {{ chart_data.revenue | tojson }},
        { tension: 0.3 }
    );
});
</script>
{% endblock %}
```

---

## Spacing & Grid Reference

### Standard Gaps

| Context | Class | Pixels |
|---------|-------|--------|
| Between KPI cards | `gap-4` | 16px |
| Between content sections | `gap-6` | 24px |
| Between cards vertically | `space-y-6` | 24px |
| Inside card padding | `p-6` | 24px |
| Inside compact card | `p-4` | 16px |
| Between form fields | `gap-4` | 16px |
| Between form sections | `space-y-6` | 24px |
| Table cell padding | `px-4 py-3` | 16px/12px |
| Filter bar padding | `p-4` | 16px |

### Standard Grid Breakpoints

| Pattern | Classes |
|---------|---------|
| 4-col KPI cards | `grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4` |
| 2-col charts | `grid grid-cols-1 lg:grid-cols-2 gap-6` |
| 3-col detail (2+1) | `grid grid-cols-1 lg:grid-cols-3 gap-6` + `lg:col-span-2` |
| 2-col form fields | `grid grid-cols-1 sm:grid-cols-2 gap-4` |
| 3-col form fields | `grid grid-cols-1 sm:grid-cols-3 gap-4` |
| Action buttons row | `flex flex-wrap items-center gap-3` |
| Sidebar stacking | `space-y-6 lg:sticky lg:top-6 self-start` |

### Standard Widths

| Element | Class |
|---------|-------|
| Max page content | Container with responsive padding |
| Form max width | `max-w-4xl` (standalone) or 2/3 of 3-col grid |
| Modal small | `max-w-md` (28rem) |
| Modal medium | `max-w-lg` (32rem) |
| Modal large | `max-w-2xl` (42rem) |
| Wizard content | `max-w-2xl mx-auto` |
| Settings input | `w-72` (18rem) in setting_row |

---

## Module Color Map (for ambient_background + macros)

Every module passes its accent colors consistently:

```python
# In each web service, set module colors in context:
MODULE_COLORS = {
    "dashboard":     {"color": "slate",   "color2": "zinc"},
    "subscribers":   {"color": "indigo",  "color2": "violet"},
    "billing":       {"color": "emerald", "color2": "teal"},
    "catalog":       {"color": "violet",  "color2": "purple"},
    "provisioning":  {"color": "amber",   "color2": "orange"},
    "network":       {"color": "blue",    "color2": "indigo"},
    "reports":       {"color": "teal",    "color2": "cyan"},
    "system":        {"color": "slate",   "color2": "zinc"},
    "notifications": {"color": "rose",    "color2": "pink"},
    "integrations":  {"color": "cyan",    "color2": "blue"},
    "radius":        {"color": "blue",    "color2": "indigo"},
    "usage":         {"color": "violet",  "color2": "purple"},
    "collections":   {"color": "emerald", "color2": "teal"},
    "vpn":           {"color": "cyan",    "color2": "teal"},
    "gis":           {"color": "green",   "color2": "emerald"},
    "resellers":     {"color": "indigo",  "color2": "violet"},
    "legal":         {"color": "slate",   "color2": "zinc"},
}
```

Used like:

```jinja2
{{ ambient_background(module.color, module.color2) }}
{{ stats_card("Label", value, icon=..., color=module.color, color2=module.color2) }}
{{ action_button("Create", href, icon_plus(), "primary", module.color, module.color2) }}
```

---

## HTMX Patterns Reference

### Filter → Table Refresh

```jinja2
{# Search input triggers table refresh #}
{{ search_input("search", search or '', "Search subscribers...", "amber", {
    "hx-get": "/admin/subscribers",
    "hx-target": "#table-container",
    "hx-trigger": "keyup changed delay:300ms",
    "hx-include": "[name='status'],[name='type']",
    "hx-push-url": "true",
}) }}

{# Filter dropdown triggers table refresh #}
{{ filter_select("status", status_options, status or '', "Status", "amber", {
    "hx-get": "/admin/subscribers",
    "hx-target": "#table-container",
    "hx-trigger": "change",
    "hx-include": "[name='search'],[name='type']",
    "hx-push-url": "true",
}) }}
```

### Pagination

```jinja2
{{ pagination(
    page=page,
    total_pages=total_pages,
    total=total,
    per_page=per_page,
    base_url="/admin/subscribers",
    hx_target="#table-container",
    color="amber"
) }}
```

### Toast Notification (from response header)

```python
# In web service after successful action:
headers = {"HX-Trigger": json.dumps({
    "toast": {"message": "Invoice created successfully", "type": "success"}
})}
return RedirectResponse(url="/admin/billing/invoices", status_code=303, headers=headers)
```

### Confirm + Delete

```jinja2
{# Button dispatches event, confirm_modal.html catches it #}
{{ danger_button(
    label="Delete",
    confirm_title="Delete Subscriber?",
    confirm_message="This will permanently remove the subscriber and all associated data.",
    action_url="/admin/subscribers/" ~ subscriber.id ~ "/delete",
    method="POST"
) }}
```

### Inline Edit (HTMX swap)

```jinja2
{# Display mode #}
<div id="field-name" hx-get="/admin/subscribers/{{ id }}/edit-name"
     hx-target="#field-name" hx-swap="outerHTML"
     class="cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-700 rounded px-2 py-1">
    {{ subscriber.name }}
</div>

{# Edit mode (returned by HTMX endpoint) #}
<form id="field-name" hx-post="/admin/subscribers/{{ id }}/update-name"
      hx-target="#field-name" hx-swap="outerHTML">
    <input name="name" value="{{ subscriber.name }}" class="...">
    <button type="submit">Save</button>
    <button hx-get="/admin/subscribers/{{ id }}/view-name"
            hx-target="#field-name" hx-swap="outerHTML">Cancel</button>
</form>
```

---

## Service Context Builder Pattern

Every web service builds context following this structure:

```python
# app/services/web_{module}.py

def build_dashboard_context(request: Request, db: Session) -> dict:
    """Build template context for module dashboard."""
    stats = core_service.ModuleManager.get_dashboard_stats(db)
    chart_data = core_service.ModuleManager.get_chart_data(db)
    recent_items = core_service.ModuleManager.list(db, order_by="created_at",
                                                    order_dir="desc", limit=5, offset=0)
    return {
        "request": request,
        "active_page": "module",
        "active_menu": "module",
        "stats": stats,
        "chart_data": chart_data,
        "items": recent_items,
        "color": MODULE_COLORS["module"]["color"],
        "color2": MODULE_COLORS["module"]["color2"],
    }


def build_list_context(request: Request, db: Session,
                       search: str = "", status: str = "",
                       page: int = 1, per_page: int = 25) -> dict:
    """Build template context for module list page."""
    offset = (page - 1) * per_page
    items = core_service.ModuleManager.list(
        db, order_by="created_at", order_dir="desc",
        limit=per_page, offset=offset,
        search=search, status=status or None,
    )
    total = core_service.ModuleManager.count(db, search=search, status=status or None)
    total_pages = (total + per_page - 1) // per_page

    return {
        "request": request,
        "active_page": "module",
        "active_menu": "module",
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "search": search,
        "status": status,
        "color": MODULE_COLORS["module"]["color"],
        "color2": MODULE_COLORS["module"]["color2"],
    }


def build_detail_context(request: Request, db: Session, item_id: str,
                         active_tab: str = "overview") -> dict:
    """Build template context for module detail page."""
    item = core_service.ModuleManager.get(db, item_id)
    related = core_service.ModuleManager.get_related(db, item_id)

    return {
        "request": request,
        "active_page": "module",
        "active_menu": "module",
        "item": item,
        "related": related,
        "active_tab": active_tab,
        "color": MODULE_COLORS["module"]["color"],
        "color2": MODULE_COLORS["module"]["color2"],
    }


def build_form_context(request: Request, db: Session,
                       item_id: str | None = None) -> dict:
    """Build template context for create/edit form."""
    item = core_service.ModuleManager.get(db, item_id) if item_id else None
    options = core_service.ModuleManager.get_form_options(db)

    return {
        "request": request,
        "active_page": "module",
        "active_menu": "module",
        "item": item,
        "is_edit": item is not None,
        "options": options,
        "errors": {},
        "color": MODULE_COLORS["module"]["color"],
        "color2": MODULE_COLORS["module"]["color2"],
    }
```

---

## Responsive Behavior Summary

| Breakpoint | Sidebar | KPI Grid | Charts | Detail Grid | Forms |
|------------|---------|----------|--------|-------------|-------|
| Mobile (<640px) | Hidden (drawer) | 1 col | 1 col stacked | 1 col stacked | 1 col |
| Tablet (640-1024px) | Hidden (drawer) | 2 col | 1 col stacked | 1 col stacked | 2 col fields |
| Desktop (1024px+) | 240px fixed | 4 col | 2 col side-by-side | 3 col (2+1) | 2-3 col fields |
| Wide (1280px+) | 240px fixed | 4 col | 2 col | 3 col (2+1) | 3 col fields |
