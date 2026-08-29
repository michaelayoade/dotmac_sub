# Selfcare Sales Orders Admin UI

## Page contract

| Item | Contract |
| --- | --- |
| Canonical route | `/admin/sales/sales-order` |
| Compatibility route | `/admin/sales/sales-orders` redirects to the canonical route |
| Primary users | Sales operations and Customer Experience staff |
| Read permission | `crm:sales_order:read` |
| Create, edit, delete permission | `crm:sales_order:write` |
| Authoritative owner | `sales.orders` (`app.services.sales_orders`) |
| Authoritative records | `SalesOrder` and `SalesOrderLine` |
| Customer source | Native `Subscriber` directory |
| Sales-agent assignment source | Active `SystemUser` records with a `SystemUserRole` grant whose canonical role is Customer Experience |
| Historical agent display source | The referenced native `SystemUser`, regardless of current activity or role eligibility |
| Plan source | Active native catalog offers |
| Inventory observation | Active field-inventory catalog items |

The CRM application was used only as an experience reference. It is not called
by this page and does not own Selfcare's native order lifecycle. The existing
native owner generates order numbers, calculates line amounts, applies payment
transitions, creates fulfillment scope and records downstream financial
effects. The form adapter invokes those owner commands and does not reproduce
those transitions.

CRM-imported ownership is translated from `crm_agents.id` through
`crm_agents.person_id` and the governed staff-map artifact to native
`SystemUser.id` values. Re-running the idempotent Phase 3 importer repairs
previously imported ownership when that mapping is available. An unresolved
reference is rendered as **Unknown agent** and retained as migration provenance
for explicit repair; UUID fragments are never used as a person label.

Manual order VAT is fixed at 7.5% and is exposed by
`app.services.sales_orders.fixed_vat_amount`; the browser calculation is a
display preview only. The server-owned result is persisted.

## Information hierarchy

The list page presents, in order:

1. Page purpose and the permission-aware Create Sales Order action.
2. Orders, gross sales, collected, outstanding, paid orders and manual orders.
3. Search, period/date, source, lifecycle, payment, agent and lead-source
   filters.
4. Sales-by-agent, payment-mix and source-mix summary panels.
5. The paginated orders table, with each order number linking to its detail page.

The detail page presents status and monetary facts first, then customer,
ownership/source, linked quote/project, line items and notes. Edit and soft
delete are shown only to users with write permission. Delete requires an impact
confirmation and retains linked customer, quote and project records.

## State coverage

- Loading: filter submission marks the table busy and displays a loading
  overlay.
- Empty: the table and each analytics section provide a scoped no-results
  message.
- Error: list and form errors are rendered as actionable alert regions; invalid
  form data returns HTTP 422 without losing the page context.
- Success: create and edit redirect with HTTP 303 to the canonical detail URL;
  delete redirects with HTTP 303 to the canonical list.

## Responsive behavior

Summary cards and filters collapse from six/four columns to two and then one.
Analytics panels stack on smaller viewports. Sales by Agent becomes compact
mobile cards with five agents shown initially and an explicit Show all/Show
fewer control. Desktop provides local agent search, shows ten matching agents
initially, and exposes Show all/Show fewer only when more than ten match. The
orders table groups date with order identity, agent with customer, lifecycle
with payment status, and all monetary facts into a financial summary. Its
desktop minimum width is bounded. On mobile the table is replaced by stacked
order cards, so horizontal scrolling is never required. Each card preserves
the order detail link, date, customer, agent, source, lifecycle and payment
states, total, collected amount and balance due. Mobile cards label Order status
and Payment status explicitly and present Source separately, so visually similar
badges cannot be mistaken for the same state dimension. Ten orders are shown initially,
with Show all/Show fewer when the current page contains more.
The table header provides a local search across the currently loaded page by
order, customer, agent, source and status; active search reveals every matching
row on that page and presents a scoped no-results state without changing the
server-owned filter or pagination contract.
Both agent and order disclosures ease their container and newly revealed
content over roughly 300ms. Reduced-motion preferences disable the scripted
height animation while preserving the same disclosure behavior.

## Validation evidence

Route/RBAC, list-query behavior, context aggregation and template compilation
are covered by `tests/test_admin_sales_web.py` and
`tests/test_web_sales_orders_list.py`. Navigation discoverability is guarded by
`tests/test_admin_nav_discoverability.py`.
