# Section 5: Splynx Finance & Billing

## Source: Splynx ISP Management Platform

This document captures feature improvement proposals for DotMac Sub's billing and finance module, derived from a detailed review of 10 Splynx Finance screenshots. Each screenshot is analyzed for the features shown, gaps in the current DotMac Sub implementation are identified, and actionable improvements are proposed.

---

## Screenshot Analysis

### Screenshot 1: Finance Dashboard (100611.png)

**Splynx Features Observed:**
- Comprehensive finance dashboard with six top-level KPI cards: Payments (count + total), Paid Invoices (count + total), Unpaid Invoices (count + total), Credit Notes (count + total), Paid Proforma Invoices, and Unpaid Proforma Invoices
- Three-column period summary (Last Month, Current Month, Next Month) showing: Payments, Paid Invoices, Unpaid Invoices, Credit Notes, Paid/Unpaid Proforma Invoices, and Total Income
- "Invoicing for period (VAT included)" bar chart with month/quarter/year toggle and multi-color legend (Paid, Unpaid, Paid on time, Paid overdue)
- "Payments for period (VAT included)" bar chart showing payment method breakdown by color (Cash CBD, Zenith 461 Bank, Paystack, QuickBooks, Zenith 523 Bank, Dollar USD, Flutterwave)
- Partner and Location filter dropdowns on each chart
- Naira (NGN) currency formatting throughout

**DotMac Sub Current State:**
The existing billing dashboard (`templates/admin/billing/index.html`) has four KPI cards (Total Revenue, Pending Amount, Overdue Amount, Collection Rate), a revenue trend area chart, an invoice status doughnut chart, quick action links, and a recent invoices table. It lacks the period-comparison summary, proforma invoice tracking, payment method breakdown charts, and multi-partner/location filtering.

### Screenshot 2: Finance Dashboard continued -- MRR, ARPU, Top 10 Payers, Daily Payments (100935.png)

**Splynx Features Observed:**
- "Monthly Recurring Revenue" line chart with period toggle (Month/Quarter/Year) and partner/location filters
- "Average Revenue per User" line chart showing ARPU trends over time
- "Top 10 payers" pie chart identifying highest-value customers by payment volume, with partner/location filters
- "Daily Payments" bar chart showing day-by-day payment collection volume with partner/location filters

**DotMac Sub Current State:**
The revenue report (`templates/admin/reports/revenue.html`) provides Total Revenue, Recurring Revenue, Outstanding, and Collection Rate KPIs, plus a revenue trend line chart. However, it lacks dedicated MRR tracking, ARPU calculations, top payers visualization, and daily payment granularity.

### Screenshot 3: Top 10 Debtors & Overdue Invoices (101013.png)

**Splynx Features Observed:**
- "Top 10 Debtors" pie chart with time filter (This month / Last month / All) and partner/location filter dropdowns, identifying the largest outstanding balances by customer name
- "Overdue Invoices" summary table with aging buckets: 0-30 days overdue (42 invoices, 14.7M NGN), 30-60 days (28 invoices, 18.4M NGN), 60-90 days (18 invoices, 9.7M NGN), 90+ days (138 invoices, 37.9M NGN), with a Total row (226 invoices, 80.7M NGN)
- Partner and location filter on overdue invoices as well
- "All" and "This year" toggle on overdue invoices

**DotMac Sub Current State:**
The AR Aging page (`templates/admin/billing/ar_aging.html`) has five aging bucket cards (Current, 1-30, 31-60, 61-90, 90+) with totals and an aging distribution chart. It lacks a "Top Debtors" visualization, does not show invoice count alongside amounts in the aging summary, and has no partner/location filtering.

### Screenshot 4: Transactions List (101059.png)

**Splynx Features Observed:**
- Dedicated "Transactions" list page under Finance, separate from invoices and payments
- Columns: Type (color-coded badges -- Debit in pink/red, Credit in green/teal), Transaction Date, Debit amount, Credit amount, Description, Category (Payment vs Service), and Actions
- Filtering bar with: Type (Any), Category (All), Period date range selector, Partners filter
- Shows transaction-level view combining both service charges (debits) and payment allocations (credits)
- Sortable columns with arrow indicators
- Row count selector ("Show 100 entries")
- Table search box

**DotMac Sub Current State:**
The General Ledger (`templates/admin/billing/ledger.html`) provides a combined transaction view with credit/debit filter pills and customer search. However, it lacks a formal transaction-type categorization (Service vs Payment), a dedicated Type badge system, configurable date range filtering, and per-partner scoping.

### Screenshot 5: Transaction Totals Summary (101125.png)

**Splynx Features Observed:**
- Summary totals footer below the transactions list showing: Credit (930 transactions, 34,093,193.16 NGN) and Debit (1,225 transactions, 58,831,250.02 NGN)
- Type column with color-coded badges (Credit in green, Debit in yellow/amber)
- Clean summary layout showing both count and amount per type

**DotMac Sub Current State:**
The ledger view does not display aggregate totals (sum of credits, sum of debits, net balance) at the bottom of the transactions list. This is a common and important accounting feature for reconciliation.

### Screenshot 6: Invoices List (101227.png)

**Splynx Features Observed:**
- Full invoices list under Finance with columns: Status (color-coded: Unpaid in blue, Paid in green, Overdue in red/orange), Customer Name, Invoice Number, Date Created, Date Due, Total, Payment Due, Payment Received, and Actions (multiple icon buttons per row)
- Filtering: text search, date period selector, status filter
- Pagination controls at the bottom
- Row-level action icons (view, edit, download PDF, email, print, delete) -- approximately 6-7 action buttons per row
- "Create invoice" and "Charge" action buttons in the header
- Status badges with strong color differentiation for quick scanning
- "Show X entries" selector

**DotMac Sub Current State:**
The invoices list page (`templates/admin/billing/invoices.html`) shows invoice number, account name, amount, due date, status badge, and a view action. It includes customer reference filtering and status pills. However, it lacks Payment Due vs Payment Received columns, inline row actions (PDF, email, print), a date range selector, and batch action capabilities.

### Screenshot 7: Invoices List continued + Totals Footer (101249.png)

**Splynx Features Observed:**
- Continuation of the invoices list showing more rows, with consistent color-coded status badges
- Pagination ("Showing X to Y of Z entries" with page navigation)
- Totals footer at the bottom summarizing by status: Unpaid (count + total), Paid (count + total), Overdue (count + total), with a grand Total row
- Each status total is highlighted in its respective badge color

**DotMac Sub Current State:**
The current invoices list shows pagination (page/total_pages) but lacks a totals summary footer that breaks down invoice counts and amounts by status. This is essential for financial oversight at a glance.

### Screenshot 8: Payment Statement Processing / Bank File Import (101442.png)

**Splynx Features Observed:**
- "Finance > Payment statements > Processing" page for importing bank statements
- Form fields: Handler (dropdown with "Base (CSV)" selected), File (Browse button), Delimiter (dropdown with "Tabulator" selected), Payment type (dropdown with "Zenith 461 Bank" selected)
- "Pair to inactive customers" toggle switch (enabled)
- Upload button
- Supports multiple delimiters (tab, comma, etc.) and multiple bank statement formats via handler plugins

**DotMac Sub Current State:**
The payment import page (`templates/admin/billing/payment_import.html`) supports CSV upload with drag-and-drop, preview table, row validation, and a template download. However, it lacks: configurable delimiter selection (hardcoded to comma), bank statement handler/format plugins, payment type/method association during import, and the ability to pair payments to inactive customers. The Splynx handler system suggests support for bank-specific statement formats.

### Screenshot 9: Finance History & Preview / Batch Invoice Generation (101520.png)

**Splynx Features Observed:**
- "Finance > History & Preview" page for batch invoice generation
- "Finance preview" section with: Date field (24/02/2026), Partners dropdown ("All selected"), "Generate separate preview per partner" toggle, Preview button
- "History" section showing previous batch runs as a table: Date, Created At, Partners, ID, Transactions (count badge), Invoices (count badge), Proforma Invoices (count), Status (badge -- "Processed"), Status Message ("Transactions have been created"), and Actions (download, retry icons)
- Period selector in the top-right (01/02/2026 - 28/02/2026)
- This represents a billing cycle run management interface

**DotMac Sub Current State:**
The invoice batch page (`templates/admin/billing/invoice_batch.html`) exists but the current implementation appears to be basic. Splynx's approach of maintaining a full history of billing runs with status tracking, transaction counts, downloadable results, and per-partner separation is significantly more mature. DotMac Sub has `app/services/billing/runs.py` and `app/tasks/billing.py` for invoice cycle runs, but the web UI for managing and reviewing batch runs could be enhanced.

### Screenshot 10: Payments List (101607.png)

**Splynx Features Observed:**
- Full payments list under Finance with columns: Payment Number (e.g., "Cash 431", "Bank 453"), Transaction Date, Amount, Note (detailed bank transfer descriptions, narrations), Customer Name (linked), and Actions (view, edit, delete icons)
- Status indicator column with color-coded indicators
- Multiple payment types visible: Cash, Bank transfers with full narration text
- Search, date period filter, partner filter
- Row-level actions per payment
- Pagination
- Rich payment descriptions showing bank transaction narratives (e.g., "NIP/WEMA/BANK/ENE CONNECTIVITY...", "FT Transfer from AFRIEXIM CORRBIAN...")

**DotMac Sub Current State:**
The payments page (`templates/admin/billing/payments.html`) shows payment stats (Total Collected, Completed, Pending, Failed) and a payments table. It includes an Import button and Record Payment action. However, it lacks the detailed bank narration display, payment type prefix labeling (Cash/Bank/Online), and the rich transaction description field that Splynx captures from bank statements.

---

## Feature Improvements

### 5.1 Finance Dashboard Enhancements

- [x] **Add period comparison summary panel** -- Display a three-column layout (Last Month / Current Month / Next Month) showing Payments, Paid Invoices, Unpaid Invoices, Credit Notes, and Total Income for each period, matching Splynx's at-a-glance period comparison
- [x] **Add Payments KPI card with count** -- Show both total payment amount and number of payments received, not just the revenue total
- [x] **Add Unpaid Invoices KPI card** -- Add a dedicated KPI card showing total unpaid invoice amount and count alongside the existing "Pending Amount" card
- [x] **Add Credit Notes KPI card** -- Display credit note count and total value as a standalone dashboard metric
- [x] **Add payment method breakdown chart** -- Create a stacked/grouped bar chart showing payments by method (bank transfer, Paystack, Flutterwave, cash, etc.) over time, with color coding per payment provider
- [x] **Add invoicing period chart with payment status overlay** -- Create a bar chart showing invoice totals per period, segmented by status (Paid, Unpaid, Paid on time, Paid overdue) with month/quarter/year toggle
- [x] **Add partner/reseller filter to all dashboard charts** -- Allow filtering all dashboard visualizations by reseller/partner and location/region for multi-tenant analytics
- [x] **Add "Planned Income" forward-looking metric** -- Calculate and display expected revenue for the next billing period based on active subscriptions and their renewal dates

### 5.2 Revenue Analytics & MRR Tracking

- [x] **Add Monthly Recurring Revenue (MRR) chart** -- Create a dedicated MRR line chart tracking the growth/contraction of recurring subscription revenue over time, calculated from active subscription values
- [x] **Add Average Revenue Per User (ARPU) chart** -- Calculate and chart ARPU trends (total revenue divided by active subscriber count) with month/quarter/year granularity
- [x] **Add Top 10 Payers visualization** -- Create a pie/donut chart showing the top 10 customers by payment volume for a given period, with partner/location filtering
- [x] **Add Top 10 Debtors visualization** -- Create a pie/donut chart on the AR Aging page showing the top 10 customers with the highest outstanding balances, with time period toggles (this month, last month, all time)
- [x] **Add Daily Payments bar chart** -- Create a day-by-day payment collection chart for the current month, useful for tracking daily cash flow patterns and identifying collection peaks
- [x] **Add MRR growth rate calculation** -- Display MRR change percentage (month-over-month) as a trend indicator on the dashboard
- [x] **Add net revenue retention metric** -- Track revenue expansion/contraction from existing customers (upgrades, downgrades, churn) as a percentage

### 5.3 Transactions & Ledger Enhancements

- [x] **Add transaction type categorization** -- Classify ledger entries into categories (Service, Payment, Credit Note, Adjustment, Refund) with dedicated filter options, matching Splynx's Category column
- [x] **Add color-coded transaction type badges** -- Display Debit entries in amber/rose badges and Credit entries in emerald/teal badges for instant visual scanning in the ledger table
- [x] **Add ledger totals summary footer** -- Display aggregate Credit total (count + amount), Debit total (count + amount), and Net Balance at the bottom of the transactions list, matching Splynx's Totals panel
- [x] **Add date range picker to ledger** -- Add a period selector (date range picker) to filter ledger entries by transaction date, replacing or supplementing the current customer-only filter
- [x] **Add Debit and Credit amount columns** -- Split the single "amount" column into separate Debit and Credit columns for traditional double-entry ledger display
- [x] **Add description/narration column to ledger** -- Include a transaction description field showing the source (e.g., bank narration, invoice reference, payment method) for each ledger entry
- [x] **Add partner/reseller filter to ledger** -- Allow filtering ledger entries by reseller/partner scope for multi-tenant financial review

### 5.4 Invoices List Improvements

- [x] **Add Payment Due and Payment Received columns** -- Show outstanding balance (Payment Due) and amount already received (Payment Received) as separate columns on the invoices list, enabling quick identification of partially paid invoices
- [x] **Add invoice totals summary footer** -- Display count and total amount grouped by status (Paid, Unpaid, Overdue, Draft) at the bottom of the invoices table, matching Splynx's totals breakdown
- [x] **Add inline row actions for invoices** -- Add PDF download, email/send, and print action buttons directly on each invoice row, rather than requiring navigation to the detail page first
- [x] **Add date range selector to invoices list** -- Allow filtering invoices by creation date or due date range using a date range picker component
- [x] **Add batch/bulk invoice actions** -- Support selecting multiple invoices and performing bulk operations: send reminders, mark as paid, void, export to PDF, or export to CSV
- [x] **Add "Charge" quick action** -- Add a "Charge" button alongside "Create Invoice" for generating one-off service charges directly from the invoices list
- [x] **Add proforma/draft invoice workflow** -- Support a proforma invoice stage before final invoicing, with separate tracking and conversion workflow, matching Splynx's Paid/Unpaid proforma invoice metrics

### 5.5 Payment Statement Import & Bank Reconciliation

- [x] **Add configurable delimiter selection** -- Allow users to choose CSV delimiter (comma, tab, semicolon, pipe) during payment import, rather than assuming comma-separated
- [x] **Add bank statement handler/format plugins** -- Create a handler system that supports different bank statement formats (e.g., Zenith Bank, GTBank, Access Bank, generic CSV), each with its own column mapping and parsing logic
- [x] **Add payment type association on import** -- Allow users to select the payment method/type (bank name, payment provider) when importing a batch, so all imported payments are tagged with the correct source
- [x] **Add "pair to inactive customers" toggle** -- Allow matching imported payments to inactive/suspended customer accounts, with an option to reactivate upon successful payment allocation
- [x] **Add bank reconciliation workflow** -- Build a reconciliation interface that matches bank statement entries against expected payments, highlighting unmatched items, duplicate detections, and partial payment scenarios
- [x] **Add import history tracking** -- Maintain a log of all payment import operations showing: date, file name, row count, matched count, unmatched count, and total amount imported
- [x] **Support tab-delimited and fixed-width formats** -- Extend the import parser to handle tab-separated and fixed-width bank statement formats commonly used by Nigerian banks

### 5.6 Billing Cycle Run Management

- [x] **Add billing run history table** -- Display a history of all invoice generation/billing cycle runs with columns: Date, Created At, Partners, Run ID, Transaction Count, Invoice Count, Status, Status Message, and Actions (download, view, retry)
- [x] **Add billing run preview mode** -- Before executing a billing run, show a preview of what will be generated (number of transactions, invoices, estimated total amount) so operators can verify before committing
- [x] **Add per-partner billing run option** -- Allow generating billing runs for specific partners/resellers separately, with a toggle for "Generate separate preview per partner"
- [x] **Add billing run download/export** -- Allow downloading the results of a billing run as a CSV/PDF report showing all generated invoices and transactions
- [x] **Add billing run status tracking** -- Track billing run status (Queued, Processing, Processed, Failed) with real-time progress indication via HTMX polling or WebSocket
- [x] **Add billing run retry capability** -- Allow retrying a failed or partially completed billing run from the history table
- [x] **Add scheduled billing run configuration** -- Allow configuring automatic billing cycle runs on specific dates (e.g., 1st of each month) with partner-specific schedules

### 5.7 Payments List Enhancements

- [x] **Add payment type prefix labels** -- Display payment method as a prefix label (e.g., "Cash 431", "Bank 453", "Online 892") in the payment number column for instant identification of payment channel
- [x] **Add bank narration/description column** -- Show the full bank transaction narration or payment description in the payments table, especially important for bank transfers where the narration contains payer identification
- [x] **Add payment note/memo field** -- Allow attaching notes or memos to individual payments for internal tracking and reconciliation purposes
- [x] **Add payment method filter** -- Add a dropdown filter to the payments list allowing filtering by payment method (Cash, Bank Transfer, Paystack, Flutterwave, etc.)
- [x] **Add date range filter to payments list** -- Add a period selector to filter payments by transaction date range
- [x] **Add payment export** -- Allow exporting the filtered payments list to CSV/Excel for external reconciliation and accounting system integration
- [x] **Add unallocated payments view** -- Create a dedicated view showing payments received but not yet allocated to any invoice, useful for identifying and resolving payment matching issues

### 5.8 Overdue Invoice Management

- [x] **Add invoice count to AR aging buckets** -- Display both the count of invoices and the total amount in each aging bucket (e.g., "42 invoices -- 14,744,477.28 NGN") for more actionable aging information
- [x] **Add "All" and "This Year" time toggles to overdue view** -- Allow toggling between all-time overdue data and current-year overdue data
- [x] **Add clickable aging bucket drill-down** -- Make each aging bucket clickable to show the list of invoices in that bucket, with customer details and last payment date
- [x] **Add partner/location filter to aging report** -- Allow filtering AR aging data by reseller/partner and geographic location
- [x] **Add overdue invoice email reminders** -- Add a "Send Reminders" bulk action from the overdue invoices view that triggers dunning emails for all overdue invoices in a selected aging bucket
- [x] **Add aging trend chart** -- Show how the aging distribution has changed over the last 6-12 months to identify whether collection is improving or deteriorating

---

## Priority Summary

### P0 -- Critical (High business impact, immediate value)

| Improvement | Section | Rationale |
|---|---|---|
| Add ledger totals summary footer | 5.3 | Essential for financial reconciliation; shows net position at a glance |
| Add invoice totals summary footer | 5.4 | Critical for financial oversight; operators need status-grouped totals |
| Add Payment Due and Payment Received columns | 5.4 | Identifies partially paid invoices without clicking into each one |
| Add billing run history table | 5.6 | Audit trail for automated billing; currently opaque |
| Add invoice count to AR aging buckets | 5.8 | Count + amount is fundamental for aging analysis |

### P1 -- High Priority (Significant operational improvement)

| Improvement | Section | Rationale |
|---|---|---|
| Add period comparison summary panel | 5.1 | Month-over-month comparison is a core finance workflow |
| Add MRR chart | 5.2 | MRR is the primary health metric for subscription businesses |
| Add ARPU chart | 5.2 | Critical for pricing strategy and growth tracking |
| Add Top 10 Debtors visualization | 5.2 | Focuses collection efforts on highest-impact accounts |
| Add bank statement handler plugins | 5.5 | Nigerian banks have varied statement formats; plugin system needed |
| Add payment type association on import | 5.5 | Tagging payment source is essential for reconciliation |
| Add billing run preview mode | 5.6 | Prevents billing errors by showing impact before execution |
| Add configurable delimiter selection | 5.5 | Tab-delimited files are common; current CSV-only is limiting |
| Add date range picker to ledger | 5.3 | Period filtering is fundamental for financial review |
| Add inline row actions for invoices | 5.4 | PDF/email/print without navigating to detail saves significant time |

### P2 -- Medium Priority (Enhanced analytics and usability)

| Improvement | Section | Rationale |
|---|---|---|
| Add payment method breakdown chart | 5.1 | Understand payment channel adoption and optimize collection |
| Add Daily Payments bar chart | 5.2 | Daily cash flow visibility for treasury management |
| Add Top 10 Payers visualization | 5.2 | Customer concentration risk identification |
| Add transaction type categorization | 5.3 | Structured categorization improves ledger usability |
| Add color-coded transaction type badges | 5.3 | Visual scanning speed improvement |
| Add batch/bulk invoice actions | 5.4 | Operational efficiency for high-volume billing |
| Add bank reconciliation workflow | 5.5 | Full reconciliation reduces manual effort |
| Add payment type prefix labels | 5.7 | Quick payment method identification in lists |
| Add bank narration column | 5.7 | Bank transfer narrations are the primary payment identifier |
| Add partner/reseller filter to charts | 5.1 | Multi-tenant analytics for reseller model |
| Add billing run per-partner option | 5.6 | Reseller-specific billing cycles |
| Add overdue invoice email reminders | 5.8 | Automated collection follow-up |
| Add payment method filter | 5.7 | Filter by channel for reconciliation workflows |
| Add unallocated payments view | 5.7 | Resolve payment matching issues efficiently |
| Add proforma invoice workflow | 5.4 | Supports quote-to-invoice conversion pattern |

### P3 -- Lower Priority (Nice to have, longer-term)

| Improvement | Section | Rationale |
|---|---|---|
| Add "Planned Income" metric | 5.1 | Forward-looking revenue forecasting |
| Add net revenue retention metric | 5.2 | Advanced SaaS-style retention analysis |
| Add MRR growth rate calculation | 5.2 | Growth velocity tracking |
| Add Debit/Credit split columns | 5.3 | Traditional double-entry display preference |
| Add description/narration column to ledger | 5.3 | Additional context per transaction |
| Add "Charge" quick action | 5.4 | Convenience shortcut for one-off charges |
| Add "pair to inactive customers" toggle | 5.5 | Edge case handling for suspended accounts |
| Add import history tracking | 5.5 | Audit trail for bulk import operations |
| Support fixed-width bank formats | 5.5 | Legacy bank format support |
| Add billing run download/export | 5.6 | Post-run reporting |
| Add billing run retry capability | 5.6 | Error recovery for failed runs |
| Add scheduled billing run configuration | 5.6 | Automation of billing cycle timing |
| Add billing run status tracking | 5.6 | Real-time progress for long-running cycles |
| Add payment export | 5.7 | External system integration |
| Add payment note/memo field | 5.7 | Internal annotation for payments |
| Add date range filter to payments list | 5.7 | Period filtering for payment review |
| Add "All" / "This Year" toggles to overdue view | 5.8 | Time-scoped overdue analysis |
| Add clickable aging bucket drill-down | 5.8 | Navigate from summary to detail |
| Add partner/location filter to aging report | 5.8 | Multi-tenant aging analysis |
| Add aging trend chart | 5.8 | Collection performance trend over time |

---

## Implementation Notes

**Existing Foundation:** DotMac Sub already has a solid billing module with invoices, payments, credit notes, AR aging, ledger, dunning, payment import, billing cycle runs, and multiple payment providers (Paystack, Flutterwave). The improvements above build on this foundation rather than replacing it.

**Service Layer:** All new dashboard statistics, MRR calculations, ARPU computations, and top debtor queries should be added to `app/services/billing/reporting.py` (the `BillingReporting` class) following the existing pattern. Web context builders go in `app/services/web_billing_overview.py` or new dedicated service files.

**Chart Integration:** The existing Chart.js integration via `DotmacCharts` (in `static/js/charts.js`) and the `chart-container` pattern should be reused for all new visualizations.

**Multi-Tenant Filtering:** Partner/reseller filtering should leverage the existing `organization_id` scoping pattern and reseller model relationships. New filter parameters should be added to service methods as keyword-only arguments.

**Bank Statement Handlers:** The plugin/handler system for bank statement formats could follow a strategy pattern with a base handler class and bank-specific subclasses registered in a handler registry, similar to the existing event handler chain pattern in `app/services/events/handlers/`.
