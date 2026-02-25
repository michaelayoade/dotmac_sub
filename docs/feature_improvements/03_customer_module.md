# Section 3: Splynx Customer Module

## Source: Splynx ISP Management Platform

This document analyzes 29 screenshots from the Splynx Customer Module and proposes feature improvements for DotMac Sub. The Splynx Customer Module covers the full customer lifecycle: profile management, service provisioning, billing configuration, usage statistics, documents, CPE management, and communication history.

---

## 3.1 Customer Detail - Information Tab

### Splynx Features Observed
- **Main information panel** (left column): Customer ID, portal login, portal password (with Show/Hide toggle), status dropdown (Active/Suspended/Canceled with color badge), billing type (Recurring/Prepaid), company name, email, billing email (separate field), phone number (multiple numbers comma-separated), partner/reseller assignment, location dropdown, street address, ZIP code.
- **Comments / To-Dos panel** (right column): Inline comment/to-do list with add (+) button directly on the customer detail page.
- **Additional information panel** (right column, collapsible): Labels (tag-style typeahead), category (Business/Residential dropdown), contact person, company ID, VAT ID, base station (GPON reference), social ID, coverage notes, comment field.
- **Account balance header bar**: Persistent blue bar showing customer name, account number, and current balance in local currency (Naira).
- **Actions dropdown**: Send message, Send welcome message, Login as customer (impersonation).
- **Tickets dropdown**: Create ticket, All tickets (with count), Opened, Closed (with count), Average answers per day statistic.
- **Prev/Next navigation arrows** for cycling through customers.
- **Map section** (bottom of Information tab): Embedded OpenStreetMap showing customer location pin, with GPS coordinates displayed.
- **Activity feed** (bottom): Recent activities log showing payment events, status changes, invoice creation with timestamps and clickable references.

### Proposed Improvements for DotMac Sub

- [ ] **Add Comments/To-Dos widget to subscriber detail page** -- Implement an inline notes/to-do panel on the right side of the subscriber detail view. Each comment should capture author, timestamp, and support a simple to-do checkbox. Use HTMX for add/toggle without full page reload. Model: `SubscriberNote` with fields `id`, `subscriber_id`, `author_id`, `content`, `is_todo`, `is_completed`, `created_at`.
- [ ] **Add persistent account balance header bar** -- Display a sticky/persistent info bar at the top of all subscriber detail tabs showing subscriber name, account number, current balance, and status badge. This gives support staff immediate financial context regardless of which tab they are viewing.
- [ ] **Add "Login as Customer" impersonation action** -- Implement admin ability to impersonate a subscriber's portal session for troubleshooting. Requires: session token generation scoped to subscriber, audit log entry on impersonation start/end, visual banner in the customer portal indicating impersonation mode.
- [ ] **Add "Send Welcome Message" action** -- Allow re-sending the welcome/onboarding email from the subscriber detail page. Pull template from notification templates, pre-fill with subscriber data.
- [ ] **Add "Send Message" quick action** -- Enable sending ad-hoc email/SMS to the subscriber directly from their detail page without navigating to a separate communication module.
- [ ] **Add integrated ticket summary on subscriber detail** -- Show ticket counts (open/closed) and quick-create link directly on the subscriber detail page header area, similar to the Splynx Tickets dropdown.
- [ ] **Add labels/tags system for subscribers** -- Implement a tagging system with typeahead search. Model: `SubscriberLabel` (many-to-many). Tags should be filterable on the subscriber list page. Use cases: VIP, corporate, problematic, referral-source, etc.
- [ ] **Add separate billing email field** -- Allow subscribers to have a distinct billing email address separate from their primary contact email. This is common for corporate accounts where invoices go to accounts@company.com but service notifications go to IT staff.
- [ ] **Add subscriber category field** -- Add a Business/Residential/Government/NGO category enum to the subscriber model. This enables segmentation in reports and different billing/dunning rules per category.
- [ ] **Add coverage notes field** -- Add a free-text field for recording signal coverage information, site survey notes, or installation constraints for the subscriber's location.
- [ ] **Add base station / PON port reference** -- Display the network infrastructure reference (OLT port, base station, PON port) directly on the subscriber information panel for quick network troubleshooting context.
- [ ] **Add GPS coordinates and embedded map to subscriber detail** -- Display an embedded map (Leaflet/OpenStreetMap) on the subscriber information tab showing the subscriber's location pin. Store latitude/longitude on the subscriber model. Consider integration with the existing GIS module.
- [ ] **Add recent activity feed to subscriber detail** -- Show a timeline of recent events (payments received, invoices generated, status changes, service orders) at the bottom of the subscriber detail page. Source from the existing audit/event system. Limit to last 10-20 items with pagination.
- [ ] **Add prev/next subscriber navigation** -- Implement previous/next arrows on the subscriber detail page header to allow cycling through subscribers in the current list context (preserving filters/sort order).
- [ ] **Add multiple phone numbers support** -- Allow storing multiple phone numbers (comma-separated or as a JSON array) with labels (primary, secondary, WhatsApp, etc.).

---

## 3.2 Customer Detail - Services Tab

### Splynx Features Observed
- **Internet services table**: Columns for ID, Status (Online/Offline badge with color), Description, Plan (clickable link to catalog), Price, Billing start date, Billing end date, Invoiced until date, Service login (RADIUS username), IPv4 address, Rule (bandwidth rule indicator), and Actions.
- **Service status indicators**: Green "Online" badge for active RADIUS sessions, showing real-time connectivity.
- **View filter dropdown**: Active / Disabled / All services.
- **Add service dropdown**: Add bundle, Add internet service, Add recurring service -- three distinct service types.
- **Rich action icons per service row**: Edit, view sessions, view traffic graph, view usage stats, view schedule, disable/block, delete -- approximately 8 action icons per row.
- **Create service modal**: Simple modal dialog with Plan dropdown selector. Additional fields appear dynamically after plan selection.
- **Table search and column configuration**: Inline table search with column show/hide controls.

### Proposed Improvements for DotMac Sub

- [ ] **Add real-time service online/offline status indicator** -- Query RADIUS accounting or session data to show whether the subscriber's service is currently online. Display as a green/red badge on the services table. Consider polling via HTMX every 30-60 seconds or using the existing RADIUS sync task data.
- [ ] **Add "Invoiced Until" column to subscriptions list** -- Show the date through which the subscriber has been invoiced for each subscription. This is critical for billing staff to see at a glance whether the subscriber is paid ahead or due for invoicing.
- [ ] **Add service login (RADIUS username) column** -- Display the RADIUS login/username associated with each subscription directly in the services table for quick network troubleshooting.
- [ ] **Add IPv4 address column to subscriptions list** -- Show the assigned IP address for each active service directly in the table. Source from RADIUS accounting or static IP assignment records.
- [ ] **Add bandwidth rule indicator** -- Show whether a bandwidth shaping/FUP rule is applied to the service. Display as a badge (e.g., "No rule", "FUP Active", "Throttled").
- [ ] **Add "Add Bundle" capability** -- Allow creating service bundles that combine multiple service types (internet + voice, internet + IPTV) as a single billable item with a combined price.
- [ ] **Add "Add Recurring Service" for non-internet charges** -- Support adding generic recurring charges (equipment rental, static IP fee, premium support) as separate line items on the subscriber's account, distinct from internet service subscriptions.
- [ ] **Add inline service action icons** -- Expand the actions column on the subscriptions table to include quick-action icons: edit, view sessions, view traffic, view usage stats, schedule change, disable/enable, delete. Use HTMX modals or slide-out panels to avoid full page navigation.
- [ ] **Add service view filter (Active/Disabled/All)** -- Add a dropdown filter above the subscriptions table to toggle between active, disabled/suspended, and all services for the subscriber.
- [ ] **Add quick service creation modal** -- Implement a modal dialog for adding a new service directly from the subscriber's Services tab. Start with plan selection, then dynamically reveal additional fields (description, custom price, start date, IP assignment) based on the selected plan.

---

## 3.3 Customer Detail - Billing Tab

### Splynx Features Observed
- **Three sub-tabs**: Finance documents, Transactions, Billing config.
- **Finance documents table**: Sortable columns for ID, Type (badge: Recurring Invoice, One-time Invoice, Proforma, Credit Note), Number, Date, Price, Payment status (Paid/Unpaid badges), Payment date, Actions. Color-coded type badges (blue for recurring, green for paid, purple for proforma).
- **Persistent billing summary bar**: Account balance, Next block date, Payment method -- always visible at the top of the billing tab.
- **Add document dropdown**: Recurring invoice, One-time invoice, Proforma invoice, Credit note, Custom payment, Future items -- six distinct document creation options.
- **Transactions table**: Transaction date, Debit, Credit, Description (service name), Category (Service/Payment), Customer ID, Type. With Show/Hide columns modal to customize which columns are visible.
- **Billing config sub-tab**: Comprehensive per-customer billing settings including:
  - Billing enabled toggle
  - Payment period (1 month dropdown)
  - Payment method (bank name dropdown)
  - Billing day (auto-document date)
  - Payment due days after document date
  - Blocking period (days after payment due)
  - Deactivation period (days after blocking)
  - Minimum balance threshold
  - Partner percent (reseller commission)
  - Auto-create invoices toggle
  - Send billing notifications toggle
- **Future Actions Preview panel**: Shows "Next block" date/status badge.
- **Billing address section**: Separate billing name, street, ZIP, city fields.
- **Reminders settings**: Enable reminders toggle, message type (Email + SMS), three configurable reminder day offsets (Reminder #1, #2, #3 with day-before-due selectors), Preview button.
- **Proforma invoice settings**: Enable auto proforma toggle, generation day, payment period, create-for period, next proforma date, Create button.
- **Payment accounts section**: Lists all available payment methods (Cash, USD, Paystack, Remita, Zenith bank accounts) with add (+) action per method.

### Invoice/Document Creation Modals Observed
- **One-time invoice modal**: Number (auto-generated), document date, payment due date, note to customer, line items table (description with typeahead, quantity, unit, price, VAT %, with-VAT amount, total), "Add more items" link, running totals (without VAT, VAT, Total, Balance due).
- **Recurring invoice modal**: Same structure as one-time but pre-populated with the service subscription line items and billing period reference.
- **Proforma invoice modal**: Identical structure to one-time invoice with separate numbering sequence.
- **Credit note modal**: Number, date, note, "Link to invoice" section with searchable invoice table (shows paid/unpaid invoices to link against), then line items for the credit.
- **Add payment modal**: Payment type (bank dropdown), date, sum, receipt number, account, transaction ID, TIN, comment for employees, note to customer, "Show other fields" expander, "Link to invoice" option.
- **Add future items modal**: Description, quantity, unit, price, VAT %, total -- items that will appear on the next recurring invoice.
- **Generate statement modal**: Date range picker, opening/closing balance display, transaction type filter, "Send to customer" action with dropdown options.

### Proposed Improvements for DotMac Sub

- [ ] **Add per-subscriber billing configuration panel** -- Implement a billing config section on the subscriber detail that allows overriding organization-level defaults: billing day, payment due days, blocking period, deactivation period, minimum balance, auto-create invoices toggle, send billing notifications toggle. Store as per-subscriber overrides that fall back to organization defaults.
- [ ] **Add "Next Block" / future actions preview** -- Show a panel on the billing tab indicating when the subscriber will be blocked/suspended based on current balance and billing rules. Display as a badge: "In the next billing cycle" or specific date.
- [ ] **Add per-subscriber reminder settings** -- Allow overriding organization-level reminder settings per subscriber: enable/disable reminders, message type (email/SMS/both), and configurable reminder day offsets (e.g., 5 days before, 2 days before, on due date).
- [ ] **Add proforma invoice support** -- Implement proforma invoices as a separate document type with its own numbering sequence. Support auto-generation based on configurable schedule. Allow conversion of proforma to final invoice.
- [ ] **Add credit note with invoice linking** -- Enhance credit note creation to allow linking against specific invoices. Show a searchable table of the subscriber's invoices (with paid/unpaid status) when creating a credit note.
- [ ] **Add "Future Items" capability** -- Allow adding line items that will automatically appear on the next recurring invoice. Use cases: one-time installation fees, equipment charges, prorated adjustments.
- [ ] **Add payment creation with invoice linking** -- When recording a payment, allow linking it to one or more specific invoices. Include fields for receipt number, transaction ID, TIN, and separate internal comment vs. customer-visible note.
- [ ] **Add statement generation** -- Implement account statement generation with configurable date range, opening/closing balance, transaction listing, and "Send to customer" action (email/download).
- [ ] **Add separate billing address** -- Allow subscribers to have a distinct billing address (name, street, ZIP, city) separate from their service/installation address.
- [ ] **Add document type badges with color coding** -- On the finance documents table, display document type as color-coded badges: Recurring Invoice (blue), One-time Invoice (indigo), Proforma (violet), Credit Note (amber), Payment (emerald).
- [ ] **Add per-subscriber payment method assignment** -- Allow assigning a default payment method per subscriber from the organization's configured payment providers. Display the assigned method prominently in the billing tab header.
- [ ] **Add partner/reseller commission percentage** -- Store a commission percentage per subscriber for reseller/partner billing. This enables automatic commission calculation on the subscriber's revenue.
- [ ] **Add blocking and deactivation period controls** -- Implement configurable grace periods: blocking period (service suspension after payment due) and deactivation period (full service termination after blocking). These cascade: due date -> blocking -> deactivation.
- [ ] **Add transaction ledger with column customization** -- Implement a transactions sub-tab showing all debits and credits with configurable columns. Allow users to show/hide columns (date, description, debit, credit, balance, category, customer ID, type) via a modal column picker.
- [ ] **Add one-time invoice creation from subscriber context** -- Allow creating ad-hoc one-time invoices directly from the subscriber's billing tab with line items, VAT calculation, and auto-generated invoice numbers.

---

## 3.4 Customer Detail - Statistics Tab

### Splynx Features Observed
- **Service type sub-tab**: "Internet" tab (implies support for multiple service types with separate stats).
- **Service and period selectors**: Service dropdown (select specific service or "All"), date range picker for the statistics period.
- **Online sessions table**: Login, data In (MB), data Out (MB), Start at (timestamp), Time (duration), IP address, MAC address, NAS device (clickable link), Actions.
- **Live bandwidth usage chart**: Real-time line graph showing Upload (pink/red) and Download (blue) bandwidth per second. Service selector dropdown and time interval selector (1 minute). Displays current Upload/Download speeds at bottom.
- **Total for period summary card**: Sessions count, Errors count, Total time (HH:MM:SS), Total Download (GB), Total Upload (GB).
- **Daily average graph**: Bar/area chart showing In speed and Out speed over time with Maximum, Average, and Last speed statistics displayed below the chart. Configurable statistics type dropdown (Daily graph, Hourly, etc.).
- **Usage by day chart**: Stacked bar chart showing Download peak and Upload peak per day, with chart/table toggle icons.
- **FUP (Fair Usage Policy) Statistics**: Traffic bonus download/upload in MB, Online key metrics. Day/Week/Month breakdown of traffic vs. bonus usage.
- **Session history table**: Detailed session log with columns for ID, Connect time, Disconnect time, Duration, Data In, Data Out, Download MB, Upload MB, IPv4 address, MAC address, NAS device. Sortable and paginated.

### Proposed Improvements for DotMac Sub

- [ ] **Add subscriber statistics tab with live bandwidth graph** -- Implement a Statistics tab on the subscriber detail page showing a real-time bandwidth usage chart. Query RADIUS accounting or VictoriaMetrics for per-subscriber traffic data. Display upload/download as a time-series line chart with configurable time intervals (1 min, 5 min, 15 min).
- [ ] **Add online sessions panel** -- Show currently active RADIUS sessions for the subscriber: login, data transferred (in/out), session start time, duration, IP address, MAC address, NAS device. Source from RADIUS accounting tables.
- [ ] **Add period summary statistics card** -- Display aggregate statistics for a selectable period: total sessions, total errors, total online time, total download (GB), total upload (GB). Use RADIUS accounting data aggregated by the existing bandwidth tasks.
- [ ] **Add daily usage chart** -- Implement a stacked bar chart showing daily download and upload usage over the selected period. Include chart/table toggle to view the same data in tabular format.
- [ ] **Add daily average bandwidth graph** -- Show a time-series chart of average bandwidth speeds with Max, Average, and Last speed statistics. Support Daily/Hourly/Weekly graph type selection.
- [ ] **Add FUP (Fair Usage Policy) statistics panel** -- If FUP/data cap policies are configured on the subscriber's plan, show current usage against allowance: traffic consumed vs. bonus/allowance, broken down by day/week/month.
- [ ] **Add session history table** -- Display a paginated, sortable table of all RADIUS sessions for the subscriber with connect/disconnect times, duration, data transferred, IP, MAC, and NAS device. Include date range filter.
- [ ] **Add per-service statistics filtering** -- When a subscriber has multiple services, allow filtering statistics by specific service or viewing aggregated data for all services.

---

## 3.5 Customer Detail - Documents Tab

### Splynx Features Observed
- **Documents table**: Columns for ID, Added (updated) by, Status, Source (Generated/Uploaded), Title, Date, Description, Actions (edit, print, download, send, delete).
- **Type filter dropdown**: Filter documents by type (All, contracts, CRM calls, etc.).
- **Upload button**: Direct file upload capability.
- **Generate / Contract button**: Generate documents from templates (contracts, agreements, service terms).
- **CRM call documentation**: Example shows a "CRM call" document type recording customer interaction notes (e.g., "CRM call to find out if they are enjoying the service. They are averaging 2mbps out of 4mbps bandwidth").
- **Table search and column configuration controls**.

### Proposed Improvements for DotMac Sub

- [ ] **Add Documents tab to subscriber detail page** -- Implement a dedicated Documents tab showing all files associated with the subscriber. Model: use existing `StoredFile` model with `entity_type=subscriber` and `entity_id=subscriber.id`. Display in a sortable table with columns: ID, uploaded by, status, source, title, date, description, actions.
- [ ] **Add document upload from subscriber context** -- Allow uploading files (contracts, ID copies, site photos, agreements) directly from the subscriber's Documents tab. Validate file type and size per the existing file upload rules.
- [ ] **Add document generation from templates** -- Implement contract/document generation using configurable templates. Templates should support variable substitution (subscriber name, address, plan details, pricing). Generate as PDF using the existing PDF export infrastructure.
- [ ] **Add CRM interaction logging** -- Allow recording customer interaction notes (phone calls, site visits, complaints) as document entries on the subscriber. Fields: type (call/visit/email/complaint), title, description, date, author. This provides a chronological customer relationship history.
- [ ] **Add document type filtering** -- Add a Type dropdown filter on the documents table to filter by document category: All, Contracts, CRM Calls, Uploaded Files, Generated Documents.
- [ ] **Add document send-to-customer action** -- Allow sending a document to the subscriber via email directly from the documents table action buttons.

---

## 3.6 Customer Detail - CPE Tab

### Splynx Features Observed
- **Add CPE section** (collapsible): Button to add a new CPE device.
- **CPE device form fields**:
  - Title (device name/label)
  - IP/Host (management IP)
  - Login (API) -- default "admin"
  - Password (API) -- with show/hide toggle
  - Port (API) -- default 8728 (MikroTik API port)
  - Type dropdown (MikroTik, and likely others)
  - QoS / Shaping toggle
  - QoS Target (subnet, e.g., 192.168.88.0/24)
  - Service ID dropdown (link CPE to a specific service)
- **Direct CPE management integration**: The form captures API credentials for remote management of customer premises equipment.

### Proposed Improvements for DotMac Sub

- [ ] **Add CPE management tab to subscriber detail** -- Implement a CPE tab showing all customer premises equipment associated with the subscriber. Display existing CPE devices in a card/table layout with status indicators.
- [ ] **Add CPE device registration form** -- Allow adding CPE devices with fields: title, IP/host, API login, API password (encrypted using credential_crypto), API port, device type (MikroTik/Ubiquiti/Generic), QoS shaping toggle, QoS target subnet, linked service ID.
- [ ] **Add CPE device type support** -- Define a `CPEDeviceType` enum supporting common ISP CPE types: MikroTik, Ubiquiti, TP-Link, Huawei, Generic. The type selection should determine available management features.
- [ ] **Add QoS/shaping configuration per CPE** -- Allow configuring bandwidth shaping parameters on the CPE device record: enable/disable toggle, target subnet, and link to the subscriber's service plan speed limits.
- [ ] **Add CPE-to-service linking** -- Allow associating a CPE device with a specific subscriber service. This enables per-service QoS enforcement and helps track which equipment serves which subscription.
- [ ] **Store CPE credentials encrypted** -- Use the existing `credential_crypto` module to encrypt CPE API passwords at rest. Display with show/hide toggle in the UI.

---

## 3.7 Customer Detail - Communication Tab

### Splynx Features Observed
- **Two sub-tabs**: Internal (email) and Messengers.
- **Internal (email) tab**: Shows email communication history with the subscriber. Lists sent emails with sender, recipient, subject, date, and preview. Includes service outage notifications, billing reminders, tariff change notices. Warning banner when SMTP is not configured.
- **Messengers tab**: Integration point for messaging add-ons (WhatsApp, Telegram, etc.). Shows a notice when no messenger add-ons are installed, with a link to the integration configuration page.

### Proposed Improvements for DotMac Sub

- [ ] **Add Communication tab to subscriber detail** -- Implement a Communication tab showing the full history of all messages sent to/from the subscriber. Sub-tabs: Email, SMS, In-App Notifications.
- [ ] **Add email communication history** -- Display all emails sent to the subscriber in a chronological list: sender, recipient, subject, date/time, delivery status, and expandable preview. Source from the existing notification delivery records.
- [ ] **Add SMS communication history** -- Display all SMS messages sent to the subscriber with delivery status tracking.
- [ ] **Add messaging integration placeholder** -- Add a Messengers sub-tab as a future integration point for WhatsApp Business API, Telegram bot, and other messaging platforms. Show a configuration notice when no integrations are active.
- [ ] **Add SMTP configuration warning** -- Display a prominent warning banner on the Communication tab when the organization's SMTP settings are not configured, with a link to system settings.

---

## 3.8 Customer Detail - DNS Security Tab

### Splynx Features Observed
- **DNS filtering/security integration**: A tab dedicated to DNS-level security (Whalebone integration). Shows per-subscriber DNS security configuration.
- **Error state handling**: Displays a clear error message when the integration is not configured ("whalebone_api_region is not set. Please check your addon config!").

### Proposed Improvements for DotMac Sub

- [ ] **Add DNS Security tab (future consideration)** -- Plan for a DNS security integration tab that could support DNS-based content filtering and threat protection services per subscriber. This is lower priority but represents a value-added service opportunity for ISPs.
- [ ] **Implement graceful integration error states** -- When integration tabs are shown but not configured, display a clear, user-friendly error message with a link to the configuration page rather than a raw error or blank page.

---

## 3.9 Cross-Cutting UI/UX Improvements

### Splynx Features Observed Across All Tabs
- **Tabbed navigation**: Information, Services, Billing, Statistics, Documents, CPE, Communication, DNS Security -- eight tabs covering all aspects of customer management.
- **Consistent header bar**: Customer name, account number, account balance always visible.
- **Column show/hide modals**: On transaction and document tables, a modal lets users toggle which columns are visible with checkboxes and drag reorder.
- **Table search**: Inline search fields on all data tables.
- **Entries per page control**: "Show 100 entries" dropdown on all tables.
- **Export and column configuration**: Export buttons (CSV, etc.) and column configuration on all major tables.

### Proposed Improvements for DotMac Sub

- [ ] **Implement tabbed subscriber detail layout** -- Reorganize the subscriber detail page into a tabbed interface with these tabs: Information, Services, Billing, Statistics, Documents, CPE, Communication. Use HTMX for tab switching without full page reload. Preserve the current URL with tab query parameter for bookmarkability.
- [ ] **Add persistent subscriber header across all tabs** -- Implement a fixed header bar that persists across all subscriber detail tabs showing: subscriber name, account number, status badge, current balance. This provides constant context for support staff.
- [ ] **Add dynamic column configuration for all data tables** -- Implement a column show/hide modal (similar to the existing `dynamic-table-config.js`) across all major data tables on the subscriber detail page. Persist user preferences per table.
- [ ] **Add table-level search for all sub-tables** -- Ensure all data tables within subscriber detail tabs have inline search/filter capability using HTMX with debounce.
- [ ] **Add entries-per-page control on all tables** -- Add a "Show N entries" dropdown (25, 50, 100) on all paginated tables within the subscriber detail view.
- [ ] **Add row-level bulk actions on data tables** -- Implement checkbox selection on table rows with bulk action toolbar (e.g., bulk send invoices, bulk mark as paid, bulk delete documents).

---

## Priority Summary

### P0 - Critical (Core subscriber management gaps)
| Improvement | Section | Rationale |
|---|---|---|
| Tabbed subscriber detail layout | 3.9 | Foundation for all other tab-based improvements |
| Persistent subscriber header bar | 3.1 / 3.9 | Essential context for support workflows |
| Per-subscriber billing configuration | 3.3 | Required for flexible billing per customer |
| Real-time service online/offline status | 3.2 | Core ISP operational need |
| Statement generation | 3.3 | Common customer and auditor request |

### P1 - High (Significant operational value)
| Improvement | Section | Rationale |
|---|---|---|
| Subscriber statistics tab with bandwidth graphs | 3.4 | Critical for support troubleshooting |
| Online sessions panel | 3.4 | Essential for diagnosing connectivity issues |
| Comments/To-Dos widget | 3.1 | Enables team collaboration on subscriber issues |
| Labels/tags system | 3.1 | Enables subscriber segmentation and filtering |
| Documents tab with upload | 3.5 | Contract and document management per subscriber |
| Credit note with invoice linking | 3.3 | Billing accuracy and audit trail |
| Communication history tab | 3.7 | Complete view of subscriber interactions |
| Invoiced-until column on services | 3.2 | Billing visibility for support staff |
| One-time invoice creation | 3.3 | Ad-hoc billing capability |
| Future items capability | 3.3 | Flexible recurring billing adjustments |

### P2 - Medium (Operational enhancements)
| Improvement | Section | Rationale |
|---|---|---|
| Per-subscriber reminder settings | 3.3 | Custom dunning per subscriber |
| Proforma invoice support | 3.3 | Prepayment workflow support |
| CRM interaction logging | 3.5 | Customer relationship history |
| CPE management tab | 3.6 | Equipment tracking per subscriber |
| Payment creation with invoice linking | 3.3 | Accurate payment allocation |
| Recent activity feed | 3.1 | Quick subscriber history overview |
| Session history table | 3.4 | Detailed usage auditing |
| Separate billing email and address | 3.1 / 3.3 | Corporate account requirements |
| Add bundle / recurring service types | 3.2 | Expanded service catalog support |
| Per-subscriber payment method | 3.3 | Default payment tracking |
| Subscriber category field | 3.1 | Business/Residential segmentation |
| Daily usage and FUP charts | 3.4 | Data cap monitoring |

### P3 - Low (Nice-to-have / Future)
| Improvement | Section | Rationale |
|---|---|---|
| Login as customer impersonation | 3.1 | Support convenience feature |
| Send welcome message action | 3.1 | Onboarding convenience |
| Prev/next subscriber navigation | 3.1 | Browsing convenience |
| GPS map on subscriber detail | 3.1 | Visual location reference |
| Document generation from templates | 3.5 | Contract automation |
| Messenger integrations | 3.7 | WhatsApp/Telegram future integration |
| DNS security tab | 3.8 | Value-added service (future) |
| QoS shaping on CPE | 3.6 | Remote CPE management |
| Transaction column customization | 3.3 | User preference convenience |
| Bulk row actions | 3.9 | Batch operations efficiency |
| Base station / PON port reference | 3.1 | Network topology context |
| Multiple phone numbers | 3.1 | Contact flexibility |
| Bandwidth rule indicator | 3.2 | FUP policy visibility |
| Partner commission percentage | 3.3 | Reseller billing automation |
| Blocking/deactivation period controls | 3.3 | Granular dunning control |

---

## Implementation Notes

### Dependencies on Existing Infrastructure
- **Statistics features (3.4)** depend on RADIUS accounting data and/or VictoriaMetrics integration already referenced in the codebase.
- **Billing features (3.3)** extend the existing billing module (`app/services/billing/`, `app/models/billing.py`).
- **Document features (3.5)** can leverage the existing `StoredFile` model and `file_storage` service.
- **CPE features (3.6)** extend the existing network equipment models (`OLT`, `ONT`, `CPE`).
- **Communication features (3.7)** build on the existing notification system (`app/tasks/notifications.py`, `app/services/email.py`).

### Architectural Considerations
- The tabbed subscriber detail layout (P0) should be implemented first as it provides the framework for all other tab-based features.
- Per-subscriber billing configuration should use a pattern of nullable override fields that fall back to organization-level defaults (similar to how `settings_spec.py` resolves settings).
- All new subscriber detail sub-pages should follow the existing thin-route pattern: `app/web/admin/subscribers.py` routes call `app/services/web_subscriber_details.py` service methods.
- Statistics charts should use a lightweight charting library compatible with HTMX (Chart.js or similar) and load data via HTMX partial requests for lazy loading.

### Data Model Additions
| Model | Purpose | Key Fields |
|---|---|---|
| `SubscriberNote` | Comments/to-dos | subscriber_id, author_id, content, is_todo, is_completed |
| `SubscriberLabel` | Tags (M2M) | subscriber_id, label_id |
| `Label` | Tag definitions | name, color, organization_id |
| `SubscriberDocument` | Document metadata | subscriber_id, file_id, type, title, description |
| `SubscriberBillingConfig` | Per-subscriber overrides | subscriber_id, billing_day, payment_due_days, etc. |
| `CPEDevice` | CPE equipment | subscriber_id, service_id, title, ip, type, credentials |
| `CommunicationLog` | Message history | subscriber_id, channel, direction, subject, status |
