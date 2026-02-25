# Section 11: Integrations & Admin Tools

## Source: Splynx ISP Management Platform

This document catalogs feature improvements for DotMac Sub based on a comprehensive review of 23 screenshots from the Splynx ISP management platform's Integrations and Tools sections. Each screenshot was analyzed for its feature capabilities and a corresponding improvement is proposed for DotMac Sub.

---

## 11.1 Integration Module Management

### Screenshot: Main Modules Toggle Dashboard
**Splynx Feature:** A centralized "Main Modules" configuration page under Config > Integrations that lets administrators enable or disable entire functional areas of the platform using toggle switches. Categories include Finance (Dashboard, Invoices, Credit notes, Transactions, Payments, Proforma invoices, History & Preview, Payment statements, Refill cards, Costs), Tariff Plans (Internet, FUP, CAP, Recurring, One-time, Bundles), Customers (Services, Additional discounts, Vouchers), Networking (Network sites, View, Customer services, Hardware, Routers, CPE/MikroTik, TR-069/ACS, Hardware, IPv4 networks, IPv6 networks), and additional top-level toggles for Inventory, Tickets, Scheduling, Voice, and Leads.

**DotMac Sub Improvements:**

- [ ] Build a "Module Manager" admin page at `/admin/system/modules` that displays all DotMac Sub functional modules (Billing, Catalog, Network, Provisioning, VPN, GIS, Notifications, Reports) as toggle-able cards
- [ ] Implement per-module feature flags stored in the `DomainSetting` model with domain `modules`, allowing granular enable/disable of sub-features (e.g., within Billing: invoices, payments, credit notes, payment statements)
- [ ] Add a Finance module toggle panel controlling visibility of: Invoice dashboard, Credit notes, Proforma invoices, Payment history, Payment statements, Refill/voucher cards
- [ ] Add a Catalog module toggle panel controlling visibility of: Internet plans, FUP policies, Bundle offers, One-time charges, Recurring charges
- [ ] Add a Customer module toggle panel controlling visibility of: Additional discounts, Voucher management, Customer services view
- [ ] Add a Networking module toggle panel controlling visibility of: Network sites, CPE management, TR-069 (GenieACS), Router management, IPv4/IPv6 network pools, Hardware inventory
- [ ] Add top-level feature toggles for: Inventory tracking, Helpdesk/Tickets, Scheduling, Voice/VoIP, Lead management CRM
- [ ] Ensure disabled modules are hidden from the admin sidebar, API routes return 404, and the customer portal hides corresponding sections
- [ ] Create a service `app/services/module_manager.py` that caches module states in Redis and provides `is_module_enabled(module_name)` helper used by route guards and template context

### Screenshot: Add-ons Marketplace (Page 1 & 2)
**Splynx Feature:** An "Add-ons" marketplace listing installable extensions with columns for Name, Version, Description, and file size. Extensions include: splynx-3cx (SIP Framework), splynx-agent (Agents add-on), splynx-ai (AI add-on), splynx-azure-sso (Azure SSO), splynx-billing-config (Billing Config), splynx-cashdesk (Cashdesk add-on), splynx-genieacs (GenieACS), splynx-google-maps-v3, splynx-mailjet, splynx-mikrotik-discoverer-and-export (MikroTik discovery), splynx-moneris (payment), splynx-netcube, splynx-paymentexpress, splynx-paypal (PayPal), splynx-paystack (Paystack), splynx-portal (Self-care portal), splynx-portalino, splynx-powercode (PowerCode), splynx-quickbooks (QuickBooks), splynx-raisecom, splynx-realms, splynx-remita (Remita), splynx-resellers (Resellers), splynx-sagepay (SagePay), splynx-speedtest, splynx-ssh-terminal, splynx-stripe (Stripe), splynx-ticket-feedback, splynx-whatsapp (WhatsApp), splynx-xero (Xero), splynx-zoho-books (Zoho Books). Each has Install/Update/Enable/Disable status badges.

**DotMac Sub Improvements:**

- [ ] Design an "Integrations Hub" page at `/admin/integrations/marketplace` showing all available and installed integration connectors as a card grid with status badges (Installed, Available, Update Available)
- [ ] Implement an integration connector model (`IntegrationConnector`) tracking: name, version, type (payment/accounting/messaging/network/crm), status (enabled/disabled/not_installed), configuration JSON, and last_sync timestamp
- [ ] Build a connector registry pattern in `app/services/integrations/registry.py` that auto-discovers connectors from `app/services/integrations/connectors/` directory
- [ ] Add version tracking and update-available indicators for each connector, with a "Check for updates" action button

### Screenshot: Install Module Form
**Splynx Feature:** A custom module installation form under Integrations > Install module, with fields for Module name, Title, Type (Simple dropdown), and a Config section with Root (dropdown for where the module appears in navigation) and Icon (Font Awesome icon code). After installation, users are redirected to an "Additional fields" page to define module properties.

**DotMac Sub Improvements:**

- [ ] Build a custom integration registration form at `/admin/integrations/register` allowing admins to register custom webhook-based integrations with: name, display title, type (simple/webhook/oauth), root navigation section, icon selection (from Heroicons library)
- [ ] After registration, redirect to a configuration page where admins define custom fields, webhook endpoints, authentication method, and data mapping rules
- [ ] Support a "Simple" integration type that embeds an external URL in an iframe within the admin panel, and a "Webhook" type that sends/receives HTTP callbacks

### Screenshot: Modules List (Installed)
**Splynx Feature:** A "Modules list" table showing 20 installed modules with columns: Name (e.g., huawei_supported_boards, splynx_huawei_module, splynx_admin_agents, splynx_admin_mailjet, splynx_cashdesk, splynx_customer_cpe_reset, splynx_olt_autodiscover, splynx_portal_social_registration, splynx_trust_agents, splynx_network_weathermap, splynx_paystack_addon, splynx_moneris_addon, splynx_referral_system, splynx_speedtest, splynx_ssh_terminal), Title, Root location, Type (Simple/Add-on), a "Relay portal status for portal" indicator, and Actions (edit/delete with color-coded enable/disable buttons in green and red).

**DotMac Sub Improvements:**

- [ ] Create an "Installed Integrations" management table at `/admin/integrations/installed` with columns: Name, Title, Root (navigation location), Type (Simple/Add-on/Webhook), Relay to Portal (yes/no toggle), Status (Enabled/Disabled badge), and Actions (edit config, disable, uninstall)
- [ ] Add bulk actions for enable/disable all selected integrations
- [ ] Show integration health status with a traffic-light indicator (green = healthy, amber = degraded/warnings, red = errors/unreachable)
- [ ] Implement an integration activity log showing last 50 API calls per connector with status codes and response times

---

## 11.2 Webhooks & Event Hooks

### Screenshot: Hooks Configuration
**Splynx Feature:** A "Hooks" management table under Config > Integrations showing 11 configured webhook/event hooks with columns: ID, Title, Type (CLI or Web), Enabled (Yes/No badge), and Actions (edit, duplicate, delete). Configured hooks include: Splynx Huawei OLT (CLI), Zoho (Web), Base Station Update (Web), tickets (Web, disabled), Splynx Referral system (CLI), WhatsApp (CLI), Splynx AI (CLI), n8n (Web), Splynx Add-on Mailjet (CLI), erpnext finance integration (Web), and Notifications (Web). Each can be individually enabled/disabled.

**DotMac Sub Improvements:**

- [ ] Build a Webhook/Hooks management page at `/admin/integrations/hooks` with a table showing all registered hooks: ID, Title, Type (CLI/Web/Internal), Enabled status, last triggered timestamp, success rate percentage, and Actions
- [ ] Support two hook types: **Web hooks** (HTTP POST to external URLs on events) and **CLI hooks** (execute local scripts/commands on events)
- [ ] Create a hook registration form with fields: title, type, URL/command, HTTP method (for web), authentication (Bearer token, Basic auth, HMAC signature), retry policy (max retries, backoff), and event filters (which DotMac events trigger this hook)
- [ ] Map hooks to the existing DotMac event system (`app/services/events/`) so any `EventType` can trigger configured hooks
- [ ] Add a hook test button that sends a sample payload and displays the response inline
- [ ] Implement hook duplication (clone an existing hook configuration)
- [ ] Add a hook execution log page showing: timestamp, event type, hook title, request payload, response status, response body, and latency
- [ ] Integrate with popular automation platforms by providing pre-built hook templates for: n8n, Zapier, Make (Integromat), and custom REST APIs

---

## 11.3 Third-Party Platform Integrations

### Screenshot: 3CX Integration (VoIP/PBX)
**Splynx Feature:** A 3CX VoIP/PBX integration page under Config > Integrations showing an embedded iframe for the 3CX configuration interface. The page has Reload and "Open in new window" action buttons. (Currently showing a 403 Forbidden error, indicating the integration is installed but the 3CX server is not accessible.)

**DotMac Sub Improvements:**

- [ ] Add a VoIP/PBX integration connector supporting 3CX and FreePBX platforms, enabling click-to-call from subscriber detail pages and automatic call logging
- [ ] Build an embedded integration frame component that loads external integration UIs within the DotMac admin panel with Reload and "Open in new window" controls
- [ ] Implement connection health monitoring for embedded integrations that shows a clear error state (with troubleshooting guidance) when the external service is unreachable

### Screenshot: QuickBooks Accounting Integration
**Splynx Feature:** A QuickBooks Accounting integration page under Config > Integrations with an embedded iframe for the QuickBooks connector configuration. Has Reload and "Open in new window" buttons. (Currently showing 403 Forbidden, indicating the connector is installed but not fully configured.)

**DotMac Sub Improvements:**

- [ ] Build a QuickBooks Online integration connector in `app/services/integrations/connectors/quickbooks.py` that syncs: invoices, payments, credit notes, and customer records bidirectionally
- [ ] Add a Xero accounting integration connector as an alternative to QuickBooks
- [ ] Add a Sage accounting integration connector for African market support
- [ ] Create a generic accounting sync framework in `app/services/integrations/accounting_sync.py` with a common interface that all accounting connectors implement: `sync_invoices()`, `sync_payments()`, `sync_customers()`, `get_sync_status()`
- [ ] Build a sync dashboard showing last sync time, records synced, errors, and a manual "Sync Now" button per accounting connector
- [ ] Implement field mapping configuration allowing admins to map DotMac fields to accounting platform fields (e.g., DotMac invoice number -> QuickBooks reference number)

### Screenshot: WhatsApp Config Integration
**Splynx Feature:** A WhatsApp Config integration page under Config > Integrations. The page shows "File not found" indicating the WhatsApp add-on is installed but the configuration file is missing. Has Reload and "Open in new window" buttons.

**DotMac Sub Improvements:**

- [x] Build a WhatsApp Business API integration connector in `app/services/integrations/connectors/whatsapp.py` supporting: template message sending, notification delivery (invoice reminders, payment confirmations, service alerts), and two-way messaging
- [x] Create a WhatsApp configuration page at `/admin/integrations/whatsapp/config` with fields: API provider (Meta Cloud API, Twilio, MessageBird), API credentials (encrypted via credential_crypto), phone number, webhook URL, and message templates
- [x] Add WhatsApp as a notification delivery channel in the existing notification system alongside email and SMS
- [x] Implement WhatsApp template message management allowing admins to create, preview, and test message templates with variable substitution (subscriber name, invoice amount, due date, etc.)

---

## 11.4 Payment Gateway Integrations

### Screenshots: Add-ons Marketplace (payment providers visible)
**Splynx Feature:** The add-ons marketplace lists multiple payment gateway integrations: PayPal, Paystack, Moneris, PaymentExpress, SagePay, Stripe, and Remita. Each is a separately installable add-on with version tracking.

**DotMac Sub Improvements:**

- [ ] Extend the existing payment provider framework (currently supporting Paystack and Flutterwave) to add Stripe as a payment gateway connector with support for card payments, bank transfers, and recurring billing via Stripe Subscriptions
- [ ] Add a PayPal integration connector supporting PayPal Checkout, PayPal subscriptions, and IPN (Instant Payment Notification) webhooks
- [ ] Add a Remita integration connector (important for Nigerian ISPs) supporting: invoice generation, payment collection, direct debit mandates, and payment status webhooks
- [ ] Add a SagePay (Opayo) integration connector for South African market ISPs
- [ ] Add a Moneris integration connector for Canadian market ISPs
- [x] Build a unified payment provider configuration page at `/admin/billing/payment-providers` showing all available gateways, their connection status, and a configuration form per provider (implemented for Paystack + Flutterwave)
- [x] Implement a payment provider test mode that allows admins to verify gateway configuration with test/sandbox credentials before going live
- [x] Add payment gateway health monitoring with automatic failover -- if the primary gateway is down, route payments to a configured secondary gateway
- [x] Create a payment reconciliation tool that compares DotMac payment records against gateway transaction reports and highlights discrepancies

---

## 11.5 Data Import Tools

### Screenshot: Import History
**Splynx Feature:** A "Config > Tools > Import" page showing an import history table with columns: ID, Module (which data type was imported), Date & Time, File (uploaded CSV/Excel), Handler (import processor used), Status (success/failed/partial), and Actions. Has a "New Import" button. Currently empty (0 entries).

**DotMac Sub Improvements:**

- [x] Build a comprehensive data import system at `/admin/system/import` with a "New Import" wizard that supports importing: Subscribers, Subscriptions, Invoices, Payments, NAS devices, IP address pools, and Network equipment
- [x] Create an import history table showing all past imports with: ID, module/entity type, timestamp, uploaded filename, handler/processor, record count (total/success/failed/skipped), status badge, and actions (view details, download error report, undo import)
- [x] Implement CSV column mapping UI where admins upload a file, see detected columns, and drag-drop map them to DotMac fields with preview of first 5 rows
- [x] Support multiple import formats: CSV (with configurable delimiter), Excel (.xlsx), and JSON
- [x] Add import validation with a dry-run mode that reports potential errors (duplicate emails, invalid statuses, missing required fields) before committing
- [x] Implement import undo/rollback capability that can reverse an import within a configurable time window (e.g., 24 hours)
- [x] Create import templates (downloadable CSV files with correct headers) for each entity type to guide users
- [x] Process large imports as Celery background tasks with progress tracking and email notification on completion

### Screenshot: Export Tool
**Splynx Feature:** A "Config > Tools > Export" page with a simple form containing: Module dropdown (currently set to "Partners"), Fields dropdown (set to "All selected" with multi-select capability), Delimiter dropdown (set to "Tabulator" with options for comma, semicolon, tab), a "First row contains column titles" toggle, and an Export button.

**DotMac Sub Improvements:**

- [x] Build a universal data export tool at `/admin/system/export` with fields: Module/Entity selector (Subscribers, Invoices, Payments, Subscriptions, NAS Devices, Service Orders, Audit Log, Users, etc.), Field selection (multi-select with "Select All" option), Delimiter (comma, semicolon, tab, pipe), Date range filter, Status filter, and "Include column headers" toggle
- [x] Support export formats: CSV, Excel (.xlsx), JSON, and PDF (for report-style exports)
- [x] Add scheduled/recurring exports that run automatically (e.g., weekly subscriber export sent to SFTP or email)
- [x] Implement export templates allowing admins to save frequently used export configurations (module + selected fields + filters) for reuse
- [x] Process large exports (>10,000 records) as background Celery tasks with download link emailed on completion
- [x] Add export audit logging to track who exported what data and when (important for POPIA/GDPR compliance)

---

## 11.6 Bulk Service Activation

### Screenshot: Activate Services Tool
**Splynx Feature:** A "Config > Tools > Activate services" page with three tabs: "Activate internet services", "Activate recurring services", and "Activate bundle services". The active tab shows a Customers filter section (Partner: Any, Status: Active, Skip active service check toggle, Ignore without IP/Additional IP toggle) and a Fields pairing section that maps source fields to target values: Plan (Plan -> 1 Mbps Fiber), Activation date (Manual input -> 24/02/2026), Router (ID -> Not selected), IPv4 assignment (None/Router will assign IP -> None), MAC (MAC(s) -> Not selected), Additional network, Login prefix (Customer login -> Login), Login suffix (Manual input), Service password (Manual input). Plus an "Other" section with "Set customers as Active on Submit" toggle.

**DotMac Sub Improvements:**

- [x] Build a "Bulk Service Activation" tool at `/admin/provisioning/bulk-activate` with tabs for: Internet services, Recurring services, and Bundle services
- [x] Add a customer filter panel allowing filtering by: reseller/partner, subscriber status, location/POP site, date range, and custom attributes
- [x] Implement a field pairing/mapping interface where admins map subscriber data fields to service activation parameters: Plan assignment, Activation date, Router/NAS assignment, IPv4 assignment method (static/dynamic/DHCP), MAC address binding, Login prefix/suffix generation, and Service password (auto-generate or manual)
- [x] Add preview mode showing all subscribers that match the filter criteria and what services will be activated, before executing
- [x] Implement batch processing with progress tracking, showing activated/failed/skipped counts in real-time via HTMX polling
- [x] Add "Set subscribers as Active on Submit" option to automatically change subscriber status upon successful service activation
- [x] Log each bulk activation as an audit event with full details of what was changed

---

## 11.7 VPN Management Tools

### Screenshot: VPN Management (WireGuard & OpenVPN)
**Splynx Feature:** A "Config > Tools > VPN" page with two tabs: "Wireguard" and "OpenVPN". The WireGuard tab shows action buttons (Refresh, Restart, Configuration, Status, Add Wireguard client) and a connections table with columns: ID, Connection name, Public Key, IP, Status, and Actions. Currently empty. This tool manages VPN tunnels between the Splynx server and remote network sites/routers.

**DotMac Sub Improvements:**

- [x] Enhance the existing WireGuard VPN module (`/admin/vpn`) to add a unified VPN management dashboard showing both WireGuard and OpenVPN connections
- [x] Add server-side VPN controls: Restart service, View configuration, Check status -- executed via Celery tasks to avoid blocking the web request
- [x] Add an "Add VPN Client" wizard that generates client configuration files (WireGuard .conf or OpenVPN .ovpn) with proper key pair generation
- [x] Implement VPN connection health monitoring with automatic alerts when a tunnel goes down
- [x] Add OpenVPN support alongside the existing WireGuard implementation for operators who prefer or require OpenVPN for legacy compatibility
- [x] Show real-time VPN tunnel statistics: uptime, bytes transferred, last handshake time, and latency

---

## 11.8 Invoice Cache Management

### Screenshot: Invoices Cache
**Splynx Feature:** A "Config > Tools > Invoices cache" page showing a simple status message: "There are 0 cached invoices." This is a cache management tool for pre-rendered invoice PDFs, allowing admins to view and clear cached invoices.

**DotMac Sub Improvements:**

- [ ] Build an invoice cache management page at `/admin/billing/cache` showing: total cached invoices, cache size on disk/S3, oldest cached invoice date, and actions to clear cache (all, by date range, by customer)
- [ ] Implement invoice PDF caching in the existing `app/services/billing_invoice_pdf.py` service, storing rendered PDFs in S3/object storage with cache invalidation when invoice data changes
- [ ] Add a "Regenerate" button per invoice that clears and re-renders the cached PDF (useful after template changes)
- [ ] Add cache statistics to the system health dashboard showing cache hit rate, storage usage, and average generation time

---

## 11.9 GPS Coordinate Management

### Screenshot: Update GPS Coordinates Tool
**Splynx Feature:** A "Config > Tools > Update GPS" page with filter controls (Period date range, Customer status dropdown set to "All selected", Rewrite existing coordinates toggle set to "No") and an "Update GPS Coordinates" action button. Below is a log table with columns: ID, Customer, Address, Status, Date created. This tool batch-geocodes customer addresses to GPS coordinates using an address-to-coordinates service.

**DotMac Sub Improvements:**

- [ ] Build a "Batch Geocode" tool at `/admin/system/tools/geocode` that geocodes subscriber addresses to GPS coordinates using a configurable geocoding provider (Google Maps, OpenStreetMap Nominatim, or Mapbox)
- [ ] Add filter controls: date range, subscriber status, "Overwrite existing coordinates" toggle (default: No, skip subscribers that already have coordinates)
- [ ] Show a geocoding log table with: subscriber name, address, resulting coordinates, status (success/failed/skipped), and timestamp
- [ ] Process geocoding as a Celery background task with progress reporting via HTMX
- [ ] Add a per-subscriber "Geocode" button on the subscriber detail page that geocodes a single subscriber's address on demand
- [ ] Integrate with the existing GIS module to automatically update map markers after batch geocoding
- [ ] Implement rate limiting to respect geocoding API quotas (configurable requests-per-second)

---

## 11.10 Customer Data Recovery

### Screenshot: Restore Deleted Customers
**Splynx Feature:** A "Config > Tools > Restore deleted customers" page with the description "Restore customers with all related data" and a search field accepting: ID, login, name, email, or phone number. A "Find" button initiates the search. This tool allows administrators to recover accidentally deleted customer records and all their associated data (services, invoices, payments, etc.).

**DotMac Sub Improvements:**

- [ ] Build a "Restore Deleted Records" tool at `/admin/system/tools/restore` that searches soft-deleted subscribers by: ID, login/username, name, email, or phone number
- [ ] Implement full cascade restoration: when restoring a subscriber, also restore their subscriptions, invoices, payments, service orders, RADIUS accounts, and network assignments
- [ ] Show a preview of what will be restored before executing (subscriber details + count of related records by type)
- [ ] Add a restoration audit log entry recording who restored the record and when
- [ ] Implement a configurable retention period for soft-deleted records (e.g., 90 days) after which they are permanently purged
- [ ] Add a "Recently Deleted" quick-view showing the last 20 deleted subscribers with one-click restore buttons
- [ ] Ensure the existing `is_active` soft-delete pattern across DotMac models supports this recovery workflow

---

## 11.11 Service Migration Tools

### Screenshots: Migrate Services (Empty + Populated)
**Splynx Feature:** A "Config > Tools > Migrate services" page with filter controls (Partner, Location, Status dropdowns) and a table showing subscribers with their current service assignments: Status (color-coded badges: Active in green, Inactive in yellow, Blocked in red), ID, Portal login, Full name, Phone number, Internet plan, IPs, Routers, MAC addresses, Base Station/OLT port. An Actions column allows migrating individual subscribers. The populated view shows ~600 subscribers with their current service details, enabling administrators to bulk-migrate services from one plan/router/base-station to another.

**DotMac Sub Improvements:**

- [ ] Build a "Service Migration" tool at `/admin/provisioning/migrate` that lists all subscribers and their current service assignments with filters for: reseller/partner, location/POP site, subscriber status, current plan, and current NAS/router
- [ ] Display a migration-ready table with columns: Status (color badge), Subscriber ID, Portal login, Full name, Phone, Current plan, Assigned IPs, Router/NAS, MAC address, OLT/Base station port, and a Select checkbox
- [ ] Implement bulk service migration actions: Change plan (move selected subscribers from Plan A to Plan B), Change router/NAS (reassign to a different NAS device), Change IP pool (reassign from one IP pool to another), Change OLT port (reassign fiber connections)
- [ ] Add a migration preview step showing exactly what will change for each selected subscriber before executing
- [ ] Process migrations as a Celery background task with rollback capability if RADIUS re-provisioning fails
- [ ] Generate a migration report after completion showing: total migrated, successful, failed (with error details), and subscribers requiring manual intervention
- [ ] Add a migration scheduling option to execute the migration during a maintenance window (e.g., 2:00 AM)

---

## 11.12 Database Administration

### Screenshot: Adminer Database Tool
**Splynx Feature:** An embedded Adminer (database administration tool) accessible from Config > Tools, gated behind a password confirmation with a warning: "By using the Adminer tool you hereby give notice that you do so at your own risk. Splynx is not responsible for any changes made to the database." Has Password field, "Go to dashboard" and "Confirm" buttons.

**DotMac Sub Improvements:**

- [ ] Add a read-only database inspection tool at `/admin/system/tools/db-inspector` that allows super-admins to view table schemas, row counts, and run SELECT-only queries against the DotMac database
- [ ] Gate access behind a separate password confirmation step with a prominent warning about data sensitivity
- [ ] Restrict to SELECT queries only (no INSERT/UPDATE/DELETE) with query validation before execution
- [ ] Add query result export to CSV
- [ ] Log all database inspection queries to the audit trail with the admin user, query text, and timestamp
- [ ] Limit access to users with a specific `system:db_admin` permission, separate from general admin permissions
- [ ] Add database statistics overview: table sizes, row counts, index usage, and slow query log summary

---

## 11.13 Speed Test Integration

### Screenshot: Speedtest Empty History
**Splynx Feature:** A "Config > Tools > Speedtest empty history" page (appears to be a Speedtest add-on configuration page showing "File not found" -- the add-on is installed but not fully configured). This feature provides an integrated speed test tool that subscribers can use to test their connection, with results logged and visible to both the subscriber and the ISP admin.

**DotMac Sub Improvements:**

- [ ] Build an integrated speed test feature accessible from the customer portal at `/portal/speedtest` using an open-source speed test library (LibreSpeed or similar)
- [ ] Store speed test results in a `SpeedTestResult` model with: subscriber_id, download_speed, upload_speed, latency, jitter, test_server, timestamp, and user_agent
- [ ] Show speed test history on the subscriber detail page in the admin panel with a chart showing speed trends over time
- [ ] Add speed test result comparison against the subscriber's plan bandwidth to flag under-performing connections
- [ ] Allow admins to view aggregated speed test analytics: average speeds by plan, by location, by time of day
- [ ] Add a "Clear speed test history" admin tool for data management

---

## Priority Summary

### P0 -- Critical (Core ISP Operations)
| # | Feature | Subsection | Rationale |
|---|---------|-----------|-----------|
| 1 | Universal Data Import/Export System | 11.5 | Essential for onboarding new ISP clients and migrating from other platforms |
| 2 | Bulk Service Activation Tool | 11.6 | Required for efficiently provisioning services during ISP rollouts |
| 3 | Service Migration Tool | 11.11 | Critical for plan changes, NAS migrations, and network restructuring |
| 4 | Payment Gateway Expansion (Stripe, Remita) | 11.4 | Directly impacts revenue collection capability across markets |

### P1 -- High (Operational Efficiency)
| # | Feature | Subsection | Rationale |
|---|---------|-----------|-----------|
| 5 | Webhook/Event Hooks System | 11.2 | Enables integration with external systems (n8n, Zapier, ERPs) without custom code |
| 6 | Module Manager with Feature Flags | 11.1 | Allows per-tenant customization and simplifies deployment for different ISP sizes |
| 7 | Accounting Integration (QuickBooks/Xero/Sage) | 11.3 | Eliminates manual double-entry and reconciliation for ISP finance teams |
| 8 | Customer Data Recovery Tool | 11.10 | Safety net for accidental deletions, reduces support burden |
| 9 | WhatsApp Business Integration | 11.3 | Primary communication channel for African market ISPs |

### P2 -- Medium (Enhanced Capabilities)
| # | Feature | Subsection | Rationale |
|---|---------|-----------|-----------|
| 10 | Batch GPS Geocoding | 11.9 | Improves GIS coverage maps and network planning accuracy |
| 11 | Invoice Cache Management | 11.8 | Performance optimization for large ISPs generating thousands of invoices |
| 12 | Integration Hub/Marketplace | 11.1 | Centralizes connector management and improves discoverability |
| 13 | VPN Management Enhancement | 11.7 | Useful for multi-site ISPs managing remote network equipment |
| 14 | Payment Reconciliation Tool | 11.4 | Reduces financial discrepancies and audit preparation time |

### P3 -- Low (Nice-to-Have)
| # | Feature | Subsection | Rationale |
|---|---------|-----------|-----------|
| 15 | Integrated Speed Test | 11.13 | Customer self-service and network quality monitoring |
| 16 | Database Inspector | 11.12 | Admin utility for troubleshooting; most operators use external tools |
| 17 | VoIP/3CX Integration | 11.3 | Niche use case for ISPs with voice services |
| 18 | Custom Module Registration | 11.1 | Extensibility for advanced operators building custom tools |

### Implementation Effort Estimates

| Priority | Feature Count | Estimated Total Effort |
|----------|--------------|----------------------|
| P0 | 4 features | 8-12 weeks |
| P1 | 5 features | 10-16 weeks |
| P2 | 5 features | 6-10 weeks |
| P3 | 4 features | 4-6 weeks |
| **Total** | **18 features** | **28-44 weeks** |

### Quick Wins (Implementable in 1-2 days each)
- Invoice cache management page (basic view + clear action)
- Export tool with CSV download for existing entity types
- "Recently Deleted" subscriber quick-view with restore button
- Webhook test button for existing event system
- Speed test history display on subscriber detail page (if speed test data exists)

---

*Document generated from analysis of 23 Splynx screenshots (10 integration + 13 tools) on 2026-02-24.*
*Target system: DotMac Sub ISP Management Platform (FastAPI/HTMX/Tailwind CSS v4).*
