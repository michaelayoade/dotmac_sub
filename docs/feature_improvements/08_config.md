# Section 8: System Configuration

## Source: Splynx ISP Management Platform

This document catalogs feature improvements for the DotMac Sub ISP management system based on a comprehensive review of 46 Splynx configuration screenshots. The screenshots cover: system-level config hub, company information, file management, templates, preferences/security, API settings, customer settings, portal configuration, email/SMTP configuration, log rotation, finance automation, billing settings, payment methods, transaction categories, accounting integration, payment pairing, billing reminders, billing notifications, tax configuration, plan change rules, RADIUS configuration, NetFlow accounting, CPE management, monitoring, MikroTik API, FUP (Fair Usage Policy), NAS types, IPv6, IP network categories, and Huawei OLT board support.

---

## 8.1 Configuration Hub & Navigation

**What Splynx shows:** A centralized Config landing page with a searchable index of all configuration sections, organized by color-coded category tabs (System, Main, Finance, Networking, Helpdesk, Scheduling, Leads, Inventory, Integrations, Tools). Each section displays clickable links to subsections with descriptive icons.

### Improvements

- [ ] **Centralized settings hub page** -- Build an `/admin/system/settings-hub` page that displays all configuration sections as categorized card groups (System, Billing, Network, Notifications, Integrations, Tools), with a global search bar that filters settings by name/description in real time using HTMX
- [ ] **Settings search with typeahead** -- Add a search input at the top of the settings hub that performs fuzzy matching across all setting names, descriptions, and category labels, returning direct links to the relevant settings section
- [ ] **Category tab filtering** -- Implement colored pill/tab buttons at the top of the settings hub (matching module accent colors from the design system: slate for System, emerald for Billing, blue for Network, rose for Notifications, violet for Catalog) to filter visible sections
- [ ] **Settings breadcrumb navigation** -- Ensure every settings sub-page has breadcrumb navigation back to the settings hub (e.g., Settings Hub > Finance > Billing Automation)
- [ ] **Quick-access settings sidebar section** -- Add a "Configuration" section in the admin sidebar that expands to show the most-used settings categories, rather than burying all settings under a single "System" menu item

---

## 8.2 Company Information & Branding

**What Splynx shows:** A Company Information page with fields for: company name, billing system URL, full address (street, ZIP, city, country, ISO country), email, phone, Company ID, VAT number, default system tax, bank details (account, name, branch), partner commission percentage, and selectable PDF templates for invoices, credit notes, proforma invoices, quotes, payment receipts, and statements. Also supports per-partner company info via a "Partners" dropdown and "Load information from another partner" feature.

### Improvements

- [ ] **Company information settings page** -- Create an `/admin/system/company-info` settings page with fields for: company name, billing URL, address (street 1, street 2, ZIP, city, country with ISO dropdown), primary email, phone numbers, company registration ID, VAT number, and bank details (account number, bank name, branch/address)
- [ ] **Default tax configuration** -- Add a "Default system tax" dropdown on the company info page that selects from configured tax rates, with a note explaining location-based tax overrides
- [ ] **PDF template selection per document type** -- Add dropdown selectors on the company info page for: Invoice PDF template, Credit Note PDF template, Proforma Invoice PDF template, Quote PDF template, Payment Receipt PDF template, and Statement PDF template, each pulling from a template library
- [ ] **Per-reseller company info overrides** -- Allow reseller/partner organizations to override company information (name, address, logo, bank details, PDF templates) so their customers see partner-branded documents
- [ ] **Partner commission percentage** -- Add a configurable commission percentage field per reseller/partner, used in revenue-sharing calculations

---

## 8.3 File Manager

**What Splynx shows:** A system-wide File Manager listing uploaded files with columns for ID, Title, Filename, Size, and Actions (view, edit, delete). Files include logos, favicons, and terms/conditions documents. Supports upload with title/filename association.

### Improvements

- [ ] **System file manager** -- Build an `/admin/system/files` page listing all system-uploaded files (logos, favicons, legal documents, branding assets) in a table with columns: title, filename, file size, upload date, and actions (preview, edit title, download, delete)
- [ ] **File categorization** -- Allow tagging files by category (Logo, Favicon, Legal Document, Template Asset, Branding) with a filter dropdown above the file list
- [ ] **File preview in modal** -- For image files, show a thumbnail preview on hover or in a modal when clicking "Preview"; for PDFs, show an embedded PDF viewer
- [ ] **Drag-and-drop file upload** -- Add a drag-and-drop zone at the top of the file manager for quick uploads, in addition to the standard upload button
- [ ] **File usage tracking** -- Show where each file is referenced (e.g., "Used in: Invoice PDF template, Customer Portal logo") to prevent accidental deletion of files in active use

---

## 8.4 Notification & Communication Templates

**What Splynx shows:** A Templates list page filtered by type (Customer portal dropdown) showing 30+ templates with ID, Title, and Description columns. Templates cover: welcome messages, new customer SMS, fiber installation progress, service restoration, maintenance notices, ticket acknowledgement/progress/resolution, link disconnection, loss of signal, compensation, payment confirmation/proof/issues, slow connection, failed appointments, installation updates, refund/refusal, account change requests, downtime compensation, support channels, and API tests. Actions include edit and delete per template.

### Improvements

- [ ] **Notification template library** -- Build an `/admin/system/templates` page listing all notification/communication templates in a searchable, filterable table with columns: name, description, category, channel (Email/SMS/In-App), last modified date, and actions
- [ ] **Template categorization by type** -- Add a dropdown filter to filter templates by type: Customer Portal, Admin Notifications, Billing, Support/Tickets, Network Alerts, Provisioning, Welcome/Onboarding
- [ ] **Template editor with variable support** -- Create a template editor form with a rich text area (or code editor for HTML templates) that shows available template variables (e.g., `{{ subscriber.name }}`, `{{ invoice.total }}`) in a sidebar reference panel
- [ ] **Template duplication** -- Add a "Duplicate" action on each template row so operators can clone an existing template and modify it rather than building from scratch
- [ ] **Template preview with sample data** -- Add a "Preview" button that renders the template with sample/mock data so the operator can see exactly what the subscriber will receive before saving
- [ ] **ISP-specific template set** -- Pre-seed templates for common ISP scenarios: welcome email, service activation, payment received, payment overdue, service suspension warning, service restored, maintenance scheduled, loss of signal (LOS) alert, installation progress update, ticket acknowledgement, and compensation notification
- [ ] **SMS template support** -- Alongside email templates, support SMS templates with character count display and variable substitution, with a note about SMS character limits

---

## 8.5 System Preferences & Security

**What Splynx shows:** A Preferences page with sections for: Default Landing Page (Administration portal vs. Customer portal), Administration & Security (admin page title, force 2FA toggle, password reset settings with code validity/attempts/method selection, email/SMS template for reset, check code character set and length), Server-side Data Table Processing (enable toggle, search delay in ms), Mention Notifications (enable with template selection for individual and group mentions), Documents Encryption (enable encryption of documents sent via email, select which documents to encrypt, password source for individual vs. business accounts), and Google OAuth credentials.

### Improvements

- [ ] **Default landing page selection** -- Add a setting to choose the default landing page when accessing the system root URL: Admin Portal, Customer Portal, or Reseller Portal
- [ ] **Admin portal title customization** -- Add a "Title for admin pages" text field in system settings that appears in the browser tab/title bar for the admin portal
- [ ] **Force two-factor authentication for admins** -- Add a toggle to require all admin users to enable 2FA, with no option for individual admins to disable it once enforced
- [ ] **Password reset policy settings** -- Create a "Password Reset" settings section with: enable/disable toggle, reset code validity period (hours), maximum reset attempts per validity period, reset method (Email or SMS), email template selector, SMS template text with variable placeholders, configurable character set for reset codes, and configurable code length
- [ ] **Server-side table processing settings** -- Add a toggle to enable/disable server-side processing for data tables across the admin UI, with a configurable search debounce delay (in milliseconds) to control HTMX search request frequency
- [ ] **@mention notification settings** -- Add toggles to enable/disable @mention notifications for individual users and group mentions, with selectable notification templates for each
- [ ] **Document encryption settings** -- Add a "Documents Encryption" section to encrypt PDF documents (invoices, statements) attached to outgoing emails, with options to select which document types to encrypt and how to derive the password (e.g., portal login, custom field, or static password per account type)
- [ ] **OAuth provider configuration** -- Add a settings section for configuring OAuth providers (Google, Microsoft) with client app name, client ID, and client secret fields for enabling OAuth-based email sending and login

---

## 8.6 API Configuration

**What Splynx shows:** A simple API settings page with fields for: System API URL, a toggle to disable API URL validation, and a toggle to trust all domains (allow requests from any domain / CORS).

### Improvements

- [ ] **API configuration page** -- Build an `/admin/system/api-config` settings page with fields for: System API base URL, CORS domain whitelist (or a "Trust all domains" toggle), and API URL validation toggle
- [ ] **API rate limiting configuration** -- Add configurable rate limits for API endpoints (requests per minute per API key), with separate limits for read vs. write operations
- [ ] **API key management** -- Add a section to manage API keys with: create new key, set permissions/scopes per key, set expiration dates, view last-used timestamp, and revoke keys
- [ ] **Webhook configuration** -- Add a webhooks settings section where operators can register external URLs to receive POST callbacks on specific events (subscriber created, payment received, service order completed, etc.)

---

## 8.7 Customer/Subscriber Settings

**What Splynx shows:** A Customers configuration page with sections for: Login (toggle for changing login on add/edit, character set for login generation, show "Generate" button, login format with variables like {id}{6}, 2FA settings with type/template selection), Password (character set, password length), Miscellaneous configs (max customers to show, statistics graph format in Bits/Bytes, default billing type), Welcome Message (enable/disable, type Email/SMS, trigger on customer status, send delay, email/SMS template selection), and Reminder Notifications (template selection for comment reminders).

### Improvements

- [ ] **Subscriber login format configuration** -- Add settings for auto-generating subscriber login usernames with a configurable format pattern using variables: `{id}`, `{email}`, `{phone}`, `{rand_number}`, `{rand_string}`, `{year}`, `{month}`, `{day}`, and a configurable character set for random generation
- [ ] **Subscriber password generation policy** -- Add settings for: available characters in generated passwords, minimum password length, and a toggle to show/hide the "Generate Password" button on subscriber forms
- [ ] **Two-factor authentication for customer portal** -- Add 2FA settings for the customer portal: enable/disable toggle, method (Email or SMS), email template selector, SMS template with code variable, configurable code character set, and code length
- [ ] **Subscriber welcome message automation** -- Add a "Welcome Message" settings section with: enable/disable toggle, delivery channel (Email, SMS, or Both), trigger condition (on status change to Active, or on creation), configurable send delay (immediately, 1 hour, 1 day, etc.), and email/SMS template selector
- [ ] **Default billing type setting** -- Add a setting to choose the default billing type for new subscribers: Recurring (Postpaid), Prepaid (Daily), or Prepaid (Custom)
- [ ] **Statistics display format** -- Add a setting to choose whether bandwidth statistics graphs display in bits per second (bps) or bytes per second (Bps)
- [ ] **Maximum search results limit** -- Add a configurable maximum number of subscribers to return in search/typeahead results to prevent performance issues on large databases

---

## 8.8 Customer Portal Configuration

**What Splynx shows:** Portal settings with two tabs (General settings and Per partner settings). General settings include: language selection, authentication field (Login/Email/Phone), password reset (enable, method, template, SMS text, code config), "Need help?" link, and Customer Mobile App (enable, Google Play App ID, App Store App ID). Per-partner settings include: portal title, GDPR toggle, menu item selection (8 of 9 selected), Dashboard toggles (show payment due, account suspension, live bandwidth usage, FUP/CAP active services, applied FUP rules), Documents (contract notification subject/text with variables), Profile field permissions (view/edit/hidden for login, name, email, billing email, phone, street, ZIP, city, payment method, password), and Internet Statistics toggles (daily usage, totals, graph, FUP stats, current limits, monthly limits/CAP, session statistics, session termination cause).

### Improvements

- [ ] **Customer portal general settings** -- Build a portal configuration page with: language/locale selector, authentication field choice (Login, Email, or Phone), password reset settings (enable, method, templates), and a configurable "Need help?" URL for the login page
- [ ] **Customer portal per-reseller branding** -- Allow per-reseller portal configuration with: custom portal title, GDPR compliance toggle, and selectable menu items to show/hide portal sections
- [ ] **Customer portal dashboard widget toggles** -- Add toggles to control which widgets appear on the customer portal dashboard: payment due amount, account suspension notice, live bandwidth usage graph, active FUP/CAP services, and applied FUP rules
- [ ] **Customer portal profile field permissions** -- Add per-field permission settings for customer profile fields (login, name, email, billing email, phone, street, ZIP, city, payment method, password) with options: View Only, Edit Allowed, or Hidden
- [ ] **Customer portal internet statistics visibility** -- Add toggles to control which statistics are visible to customers: daily usage graphs, period totals, bandwidth usage graph, FUP statistics, current speed limits, monthly data limits (CAP), session statistics, and session termination cause
- [ ] **Customer portal contract/document notifications** -- Add configurable subject and body templates for contract-related notifications sent via the portal, with variable support (`{{ contract.title }}`, `{{ company.name }}`)
- [ ] **Mobile app integration** -- Add settings to configure mobile app download links (Google Play App ID, App Store App ID) shown on the customer portal login page

---

## 8.9 Email & SMTP Configuration

**What Splynx shows:** An Email settings page with per-partner configuration. Sections include: Email Address (sender name, sender email, admin email, enable email sending toggle, redirect all emails to test address, BCC copy address, days until email expiration, emails limit per hour), Transport (transport type: SMTP, set local domain toggle), and SMTP Config (host, port, verify SSL certificate, encryption method: SSL, use authentication toggle, username).

### Improvements

- [ ] **Email sending configuration** -- Build an `/admin/system/email-config` settings page with: sender display name, sender email address (From), admin notification email, and a master enable/disable toggle for outbound email
- [ ] **SMTP transport settings** -- Add SMTP configuration fields: host, port, encryption method (None, SSL, TLS/STARTTLS), verify SSL certificate toggle, authentication toggle with username and password (encrypted with credential_crypto)
- [ ] **Email rate limiting** -- Add an "Emails per hour" limit setting to prevent exceeding SMTP provider rate limits, with 0 meaning unlimited
- [ ] **Email testing/debug mode** -- Add a "Redirect all emails to" field that, when set, routes all outgoing emails to a single test address instead of actual recipients (for staging/testing)
- [ ] **Email BCC for compliance** -- Add a "Copy email (BCC)" field to BCC all outgoing emails to a compliance or archive address
- [ ] **Email expiration** -- Add a "Days until expiration" setting for queued emails, after which unsent emails are discarded from the outgoing queue
- [ ] **Per-reseller email settings** -- Allow resellers to configure their own sender name, sender email, and SMTP credentials so emails to their customers come from the reseller's domain
- [ ] **Email delivery log link** -- Show a link to the email delivery log from the email config page so operators can quickly check delivery status after configuration changes

---

## 8.10 Log Rotation & Data Retention

**What Splynx shows:** A Logrotate settings page with configurable retention periods for: admin logs (6 months), API logs (1 month), internal logs (2 years), portal logs (1 year), customer statistics (3 years), voucher statistics (1 year), unused RRD files (1 year), background task logs (3 months), and background task result files (2 years). Each setting has a dropdown for time period and a reset button.

### Improvements

- [ ] **Data retention policy settings** -- Build an `/admin/system/data-retention` settings page with configurable retention periods for: admin audit logs, API request logs, internal system logs, portal access logs, subscriber usage statistics, background task logs, and background task result files
- [ ] **Retention period dropdowns** -- Provide dropdown selectors with predefined retention options (1 month, 3 months, 6 months, 1 year, 2 years, 3 years, 5 years, Indefinite) for each log/data category
- [ ] **Automated cleanup Celery task** -- Create a scheduled Celery task that runs daily/weekly to purge data older than the configured retention periods, with a summary report logged after each run
- [ ] **Reset to defaults** -- Add a "Reset" button next to each retention setting that restores the system default retention period
- [ ] **Compliance note** -- Display a warning banner on the data retention page noting that retention policies should comply with local regulatory requirements (e.g., GDPR, local telecom regulations)

---

## 8.11 Finance Automation

**What Splynx shows:** A Finance Automation page (Config > Finance > Automation) with sections for: Finance Automation (enable auto-issuing of transactions/invoices/proforma invoices, confirmation period in days, confirmation time, preview generation days before billing, date to use on documents: billing date vs. real date, dashboard notification toggle, per-partner preview toggle), Automatic Blocking (enable blocking period processing, blocking time, enable deactivation period, prepaid deactivation toggle, block by one-time invoices, block on weekends toggle, block on holidays toggle), and Automatically Remove IPs for Inactive Prepaid Customers (enable, period in months).

### Improvements

- [ ] **Invoice auto-generation settings** -- Add a "Finance Automation" settings section with: enable/disable automatic invoice issuing, confirmation period (days before auto-confirming draft invoices), confirmation time of day, preview generation offset (days before billing day), and date mode selection (billing date vs. actual issuance date)
- [ ] **Automatic service blocking rules** -- Add an "Automatic Blocking" settings section with: enable blocking period processing, blocking execution time (time of day), enable deactivation period processing, separate toggle for prepaid customer deactivation, toggle for blocking on one-time invoices, and toggles to process blocking on weekends and holidays
- [ ] **Dashboard billing notification** -- Add a toggle to show/hide a dashboard notification banner on the billing confirmation day, alerting admins that invoices are ready for review
- [ ] **Automatic IP reclamation** -- Add settings to automatically release IP addresses and archive service credentials for inactive prepaid customers after a configurable period (in months), freeing resources back to the pool
- [ ] **Per-partner preview generation** -- Add a toggle to generate billing previews separately for each reseller/partner organization
- [ ] **Holiday-aware blocking** -- Integrate with the localization/holiday calendar so that automatic blocking respects configured public holidays and weekends

---

## 8.12 Billing & Invoice Settings

**What Splynx shows:** A Finance Settings page (per-partner) with sections for: Recurring Billing (enable, payment period, payment method, billing day with calendar picker showing billing/blocking/deactivation timeline, use customer creation date toggle, payment due days, blocking period days, deactivation period days, minimum balance, send billing notifications toggle), Prepaid Custom settings (issue invoice after payment, item text, tax, auto-associate invoice with payments), Prepay settings (deactivation period), Receipt settings (receipt number format with variables), Invoice settings (auto-create toggle, invoice number format with variables, invoice cache toggle, enable zero-total invoicing, auto-associate with payments), Proforma Invoice settings (enable auto proforma, generation day, payment period, create for next month, number pattern), and Credit Note settings (number pattern with variables).

### Improvements

- [ ] **Recurring billing configuration** -- Add a billing settings page with: billing enabled toggle, payment period (monthly/quarterly/annual), default payment method selector, billing day of month (1-28) with a visual calendar showing the billing/blocking/deactivation timeline, toggle to use customer creation date as billing day, payment due period (days after invoice), blocking period (days after due date or "Same as due date"), deactivation period (days after blocking or "Disabled"), minimum balance threshold, and send billing notifications toggle
- [ ] **Billing day visual calendar** -- Display an interactive calendar widget showing the billing day highlighted in green, blocking period in amber, and deactivation period in rose, helping operators visualize the customer lifecycle timeline
- [ ] **Prepaid billing settings** -- Add a "Prepaid" settings section with: issue invoice after payment toggle, default line item text, default tax rate, auto-associate invoices with payments, and deactivation period for prepaid accounts
- [ ] **Invoice numbering configuration** -- Add configurable invoice number format with template variables: `{year}`, `{month}`, `{day}`, `{partner_id}`, `{customer_id}`, `{location_id}`, `{rand_number}`, `{rand_string}`, `{next}` (auto-increment), and `{var|length}` for zero-padded numbers
- [ ] **Receipt numbering configuration** -- Separate receipt number format configuration with the same variable system, plus `{type}` for payment type
- [ ] **Proforma invoice automation** -- Add settings for: enable auto proforma invoice generation, generation day of month, proforma payment period, create proforma for current/next month, and proforma number pattern
- [ ] **Credit note numbering** -- Add configurable credit note number format (e.g., `CN{year}{partner_id|2}{next|6}`) with the same variable system
- [ ] **Zero-total invoice toggle** -- Add a toggle to allow/prevent generation of invoices with a zero total amount
- [ ] **Invoice caching** -- Add a toggle to enable PDF invoice caching for performance (generate once, serve cached copy)
- [ ] **Bulk update existing customers** -- When changing billing settings that only apply to new customers, show a prominent "Update existing customers" button with a confirmation dialog to apply changes retroactively

---

## 8.13 Payment Methods & Providers

**What Splynx shows:** A Payment Methods list page showing 10 configured methods: Cash CBD, Zenith 461 Bank, Paystack, Refill card, QuickBooks, Zenith 523 Bank, Dotmac USD, Flutterwave, UBA, and Remita. Each has ID, Name, Active status (Yes/No), and edit/delete actions. Add button at top right.

### Improvements

- [ ] **Payment methods management page** -- Build a payment methods settings page listing all configured payment methods in a table with: name, active status badge, and edit/delete actions, plus an "Add" button
- [ ] **Payment method form** -- Create a form to add/edit payment methods with: name, active/inactive toggle, payment type (Cash, Bank Transfer, Online Gateway, Card, Voucher/Refill), and optional integration settings (API keys, merchant IDs) stored encrypted
- [ ] **Payment method ordering** -- Allow drag-and-drop reordering of payment methods to control the display order in customer-facing payment forms
- [ ] **Online payment gateway integration settings** -- For gateway-type payment methods (Paystack, Flutterwave, Remita), show additional fields for API credentials, callback URLs, and a "Test Connection" button
- [ ] **Payment method usage statistics** -- Show a small usage count or percentage next to each payment method indicating how many active customers use it, helping operators understand which methods are most popular

---

## 8.14 Transaction Categories & Accounting Integration

**What Splynx shows:** A Transaction Categories page with a list of categories (Service, Discount, Payment, Refund, Correction as defaults, plus custom: Credit note, Air Fibre Installation Cost, Call down support, Faulty Device Replacement, Dropcable Rerun, IP Addresses, Ground Fibre Installation Cost, Relocations, Withholding Tax, Stampduty deducted from sales). Below the list, a "Transaction categories configuration" section maps categories to service types (Internet service, Recurring service, One-time service, Bundle service) with dropdowns for Service/Discount/Top-up/Activation fee/Cancellation fee. Also includes "Miscellaneous configs" mapping inventory/invoice/credit note items to categories. A separate section enables transaction categories per tariff plan type.

Also shows an Accounting Categories page for mapping transaction categories to external accounting software categories (e.g., QuickBooks), with a "Load categories" button to sync from accounting software, and an Accounting Tax Rates page mapping internal tax rates to accounting software tax rates.

### Improvements

- [ ] **Transaction category management** -- Build a transaction categories settings page with a list of categories (with default/system categories marked and non-deletable), plus the ability to add custom ISP-specific categories like: Installation Cost, Equipment Replacement, IP Address Fees, Relocation Fees, Withholding Tax, Stamp Duty, Credit Notes
- [ ] **Transaction category mapping per service type** -- Add a mapping configuration that assigns default transaction categories to each service type context: Internet service (service, discount, top-up), Recurring service (service, discount), One-time service (service), and Bundle service (service, discount, activation fee, cancellation fee)
- [ ] **Enable transaction categories per plan type** -- Add toggles to enable/disable transaction category selection in tariff/plan edit forms, per plan type (Internet, Recurring, One-time, Bundle)
- [ ] **Accounting software category mapping** -- Add an accounting integration section that maps internal transaction categories to external accounting software categories (e.g., QuickBooks, Xero), with a "Load categories" button to pull available categories from the connected accounting platform
- [ ] **Accounting tax rate mapping** -- Add a tax rate mapping page that links internal tax rates to external accounting software tax codes, with a "Load tax rates" button to sync from the accounting platform
- [ ] **Accounting category sync status** -- Show a "Last synced" timestamp and "Reload" button for accounting categories and tax rates, with visual indicators for unmapped items that need attention

---

## 8.15 Payment Pairing & Reconciliation

**What Splynx shows:** A Pairing settings page with dropdowns to configure how imported bank payments are matched to customers and invoices: Global pairing fields (#1: Customer ID, #2: None), Invoice pairing field (Invoice number), and Proforma Invoice pairing field (Proforma invoice number).

### Improvements

- [ ] **Payment pairing/reconciliation rules** -- Add a settings page for configuring how imported bank payments are automatically matched to customers: primary match field (Customer ID, Account Number, Phone, Email), secondary match field (optional), invoice match field (Invoice Number), and proforma invoice match field
- [ ] **Smart payment matching** -- Implement fuzzy matching logic that attempts multiple pairing strategies in order: exact invoice number match, exact customer ID match, amount + date match, then flagging unmatched payments for manual review
- [ ] **Unmatched payment queue** -- When automatic pairing fails, route the payment to an "Unmatched Payments" queue where operators can manually pair them to customers/invoices

---

## 8.16 Billing Reminders

**What Splynx shows:** A Reminders settings page (per-partner, with General/Per partner tabs) with: enable reminders toggle, message type (Email + SMS), send time, and three configurable reminder waves. Each wave has: days before due date (e.g., 5, 2, Disabled), email subject, email template selector, SMS template selector. Additional options: filter by payment methods, attach unpaid invoices to reminder emails, and a calendar visualization of billing day vs. reminder schedule.

### Improvements

- [ ] **Multi-wave billing reminder system** -- Build a billing reminders settings page with: master enable toggle, delivery channel (Email, SMS, or Both), send time of day, and up to 3 configurable reminder waves
- [ ] **Per-wave reminder configuration** -- For each reminder wave, configure: days before/after due date (or "Disabled"), email subject line, email template selector, and SMS template selector
- [ ] **Reminder payment method filter** -- Add a toggle to send reminders only to customers using specific payment methods (e.g., skip reminders for auto-debit customers), with a multi-select for applicable methods
- [ ] **Attach unpaid invoices to reminders** -- Add a toggle to automatically attach the relevant unpaid invoice PDF to reminder emails
- [ ] **Per-reseller reminder settings** -- Allow resellers to override the global reminder settings with their own wave timing, templates, and send preferences
- [ ] **Reminder schedule calendar view** -- Display a visual calendar showing the billing day, each reminder wave, blocking date, and deactivation date on a timeline, so operators can see the full dunning schedule at a glance

---

## 8.17 Billing Notifications (Prepaid/Recurring)

**What Splynx shows:** A Finance Notifications page with tabs for: Global, Recurring, Prepaid (Daily), Prepaid (Custom), Card Expiration, Services, and Contracts. The Prepaid (Custom) tab shows: main settings (hour to send), Blocking Wave (enable, send to Email/SMS, email/SMS template, BCC), First Notification Wave (enable, days before blocking, send to, email/SMS template, BCC), and Second Notification Wave (same structure). Each wave is independently configurable.

### Improvements

- [ ] **Multi-channel billing notification waves** -- Build a billing notifications settings page with separate tabs for notification contexts: Recurring Billing, Prepaid Billing, Card Expiration, Service Events, and Contract Events
- [ ] **Prepaid notification wave configuration** -- For prepaid billing, configure: notification send time (hour of day), a "Blocking wave" (sent on the day of blocking with email/SMS template), and multiple pre-blocking warning waves (e.g., 5 days before, 1 day before) each with: enable toggle, days before blocking, channel (Email/SMS/Both), template selectors, and optional BCC address
- [ ] **BCC per notification wave** -- Add a BCC email field per notification wave so specific stakeholders (e.g., finance team, account managers) can be copied on blocking or overdue notifications
- [ ] **Notification scheduling** -- Configure the exact time of day notifications are sent, separate from the blocking execution time, to ensure customers receive advance notice at a reasonable hour

---

## 8.18 Tax Configuration

**What Splynx shows:** A Taxes page listing configured tax rates: Tax 0.00% (0%), 2019 VAT (5%), Tax 7.50% (7.5%). Each has name, rate, optional group name, and edit/delete actions. Supports adding individual taxes and tax groups. An "Active" filter dropdown allows filtering active vs. archived taxes.

### Improvements

- [ ] **Tax rate management page** -- Build a tax configuration page listing all tax rates with: name, rate percentage, group name (optional), active/archived status badge, and edit/delete actions
- [ ] **Add individual tax rates** -- Support creating tax rates with: name, percentage rate, optional tax group assignment, and active/archived status
- [ ] **Tax groups** -- Support tax groups that combine multiple tax rates (e.g., "State Tax" + "Federal Tax" grouped as "Combined Tax"), applied as a single selection on invoices
- [ ] **Archive/restore taxes** -- Instead of hard-deleting tax rates (which may be referenced by historical invoices), support archiving with a filter to show Active, Archived, or All
- [ ] **Location-based tax rules** -- Add support for location-based tax overrides where different regions/states can have different default tax rates, automatically applied based on subscriber location

---

## 8.19 Plan Change / Service Upgrade Configuration

**What Splynx shows:** A Change Plan settings page (per-partner) with sections for Recurring and Prepaid (Custom) billing types. Each section has: plan change refund policy (Refund unused money / No refund / Do not create transaction), additional fee for upgrading to a more expensive plan, additional fee for downgrading, tax rate for additional fees, and timing for invoice creation after service change (During next billing cycle / Immediately). Prepaid also includes rollover expiration setting (End of period). A Discounts section has a toggle to transfer discounts to new services when plans change.

### Improvements

- [ ] **Plan change refund policy** -- Add a settings section for configuring what happens when a subscriber changes plans mid-cycle: "Refund unused money" (prorate and credit), "No refund" (forfeit remaining period), or "Do not create transaction" (silent switch)
- [ ] **Plan change fees** -- Add configurable fees for: upgrading to a more expensive plan (flat amount), downgrading to a less expensive plan (flat amount), and a tax rate to apply to these fees
- [ ] **Invoice timing on plan change** -- Add a setting for when to generate the invoice for the new plan: "During next billing cycle" or "Immediately on change"
- [ ] **Prepaid plan change rollover** -- For prepaid subscribers changing plans, add a "Rollover expiration" setting controlling whether unused credit/time carries over: "End of period", "Immediate", or "No rollover"
- [ ] **Discount transfer on plan change** -- Add a toggle to automatically transfer active discounts from the old service to the new service when a subscriber changes plans
- [ ] **Per-reseller plan change rules** -- Allow resellers to override plan change policies for their customer base
- [ ] **Minimum invoice amount** -- Add a "Minimum invoice amount" field to suppress invoice generation for trivially small prorated amounts

---

## 8.20 RADIUS Configuration

**What Splynx shows:** RADIUS configuration with General and Advanced tabs. General tab: Reject IP addresses (5 configurable ranges for different rejection reasons: user not found, blocked/inactive, negative balance, incorrect MAC, incorrect password), NAS config (NAS type selector with "Load" to pull config templates), and RADIUS Tools (Restart radius, Clear online sessions buttons). Advanced tab: Radd Server (listen IP, port), Debug/Logs (short log toggle, file path, debug toggle with auto-off after 60 min, debug level 0-10, console/syslog/file output toggles), RADIUS Extended (check online duplicate sessions, DHCP framed-route, bind MAC on first connect, max unique MACs, overwrite oldest MAC), Administrative Access (allow unknown NAS devices, default NAS ID), RADIUS NAS Settings (force network to use one NAS, static IP on connect), IP from Pools (link to customer location, use "Location = All" fallback), and Periodic RADIUS Server Restart (enable, frequency weekly/monthly, time of day).

### Improvements

- [ ] **RADIUS reject IP configuration** -- Add a RADIUS settings section for configuring reject IP ranges assigned to subscribers based on authentication failure reason: user not found, account blocked/inactive, negative balance/overdue, incorrect MAC address, and incorrect password. Each range helps operators diagnose authentication issues
- [ ] **RADIUS NAS type templates** -- Add a NAS type selector that loads pre-configured RADIUS attribute templates for specific NAS vendors (MikroTik, Cisco, Ubiquiti, etc.)
- [ ] **RADIUS debug/logging controls** -- Add RADIUS debug settings with: enable debug toggle (auto-disables after 60 minutes to prevent log bloat), debug verbosity level (0-10), output destinations (file, console, syslog), and a link to view the debug log file
- [ ] **RADIUS session management** -- Add utility buttons to: restart the RADIUS server and clear all online sessions, with confirmation dialogs warning about service impact
- [ ] **RADIUS MAC binding** -- Add settings for MAC address binding: bind MAC on first connect toggle, maximum unique MAC addresses per service, and toggle to overwrite the oldest MAC when the limit is reached
- [ ] **IP pool location linking** -- Add a toggle to link IP pool assignment to subscriber location, with a fallback to "Location = All" pools when no location-specific pool matches
- [ ] **Periodic RADIUS restart schedule** -- Add settings for automatic periodic RADIUS server restarts to prevent memory leaks: enable toggle, frequency (weekly/monthly), day of week/month, and time of day
- [ ] **RADIUS administrative access** -- Add a toggle to allow/deny administrative access from unknown NAS devices, with a configurable default NAS ID for unrecognized sources

---

## 8.21 NetFlow Accounting

**What Splynx shows:** A NetFlow Accounting settings page with: Accounting Options (max timeout for idle sessions in seconds, max session time in hours to split long sessions, minimum bytes threshold for recording), Daemon Options (rotation interval in seconds, listen port), and Expire Options (max file lifetime, lifetime unit in weeks).

### Improvements

- [ ] **NetFlow accounting configuration** -- Add a NetFlow settings page with: max session idle timeout (seconds), max session duration before splitting (hours), minimum bytes threshold for recording traffic, daemon rotation interval (seconds), NetFlow listen port, and data file retention settings (max lifetime and unit)
- [ ] **NetFlow data retention** -- Add configurable retention for NetFlow data files with automatic cleanup of expired files to manage disk usage

---

## 8.22 CPE (Customer Premises Equipment) Configuration

**What Splynx shows:** A CPE configuration page with sections for: Size (1KB = 1000 or 1024), API (debug toggle, connection attempts, timeout), QoS (reverse in/out toggle, queue types for download/upload, default QoS rules with format specification, simple target network), WLAN (enable WLAN management toggle), Customer Blocking (enable blocking on CPE via NAT rules, redirect IP, redirect port), and DHCP Default Config (enable, server name, interface, lease time, network/gateway/pool range, DNS servers, WINS servers).

### Improvements

- [ ] **CPE management configuration** -- Add a CPE settings page with: bandwidth unit base (1000 vs. 1024 for KB), MikroTik API connection settings (debug toggle with auto-off, connection attempts, timeout seconds)
- [ ] **QoS configuration** -- Add a QoS settings section with: reverse in/out toggle (for correcting traffic direction), default queue types for download and upload, custom QoS rules editor (with format: Name, Network, In, Out, Limit-at, Priority), and default simple queue target network
- [ ] **CPE-based customer blocking** -- Add settings for blocking customers via CPE NAT rules: enable toggle, redirect IP address (captive portal/walled garden), redirect port, allowing blocked customers to see a "payment required" page instead of losing all connectivity
- [ ] **Default DHCP configuration template** -- Add a DHCP default config section for provisioning CPE devices: enable toggle, DHCP server name, interface, lease time (minutes), network/gateway/pool range, DNS servers, and WINS servers
- [ ] **WLAN management toggle** -- Add a toggle to enable/disable wireless LAN management features for supported CPE devices

---

## 8.23 Monitoring Configuration

**What Splynx shows:** A Monitoring configuration page with three manageable lists: Vendors (MikroTik, Cisco, Ericsson, D-Link, Juniper, Ubiquiti, TP-Link, Other), Device Types (Router, Switch, Server, Other, Access Point, CPE), and Groups (Main, Alerts -- note: "Notifications can be configured per group"). Each list supports add, edit, and delete operations.

### Improvements

- [ ] **Network monitoring vendor registry** -- Build a monitoring configuration page with a manageable list of equipment vendors (MikroTik, Cisco, Ubiquiti, Huawei, TP-Link, etc.) used for categorizing monitored devices
- [ ] **Monitoring device type registry** -- Add a configurable list of device types (Router, Switch, Server, Access Point, CPE, OLT, ONT, Firewall) used to classify monitored network equipment
- [ ] **Monitoring notification groups** -- Add configurable monitoring groups (e.g., "Core Infrastructure", "Access Layer", "Customer CPE") with per-group notification settings, allowing different escalation policies for different equipment tiers
- [ ] **Monitoring alert thresholds** -- Add configurable default thresholds per device type for: CPU usage, memory usage, interface utilization, uptime, and response time, triggering alerts when thresholds are exceeded

---

## 8.24 MikroTik API Configuration

**What Splynx shows:** A MikroTik API settings page with: Size (1KB definition), API (debug toggle with 60-min auto-off, connection attempts, timeout), Accounting (log file toggle, min bytes, max timeout, max session time, RRD enable/cached toggles), Router Accounting Options (account local traffic toggle, accounting table threshold), Simple Shaping (reverse in/out, queue types for download/upload), PCQ Shaping (shaping in/out chains, routing in/out chains, for-radius toggle, connection mark toggle), PPP Secrets (add Caller ID for MAC restriction), and IP Firewall Filter/DHCP (add framed route, include inactive customers, filter rules template with MikroTik CLI commands, filter rules position, system IP/hostname, allowed resources address list name and addresses). Also has a "Mass update all routers" button.

### Improvements

- [ ] **MikroTik API integration settings** -- Add a dedicated MikroTik API configuration page with: API debug mode (auto-off after 60 min), connection attempts, timeout, and accounting options (min bytes, max timeout, max session time, RRD graph generation)
- [ ] **Bandwidth shaping configuration** -- Add shaping method settings for: Simple Queue shaping (reverse in/out, queue types), PCQ shaping (chain definitions, routing, RADIUS integration), and configurable defaults for download/upload queue types
- [ ] **MikroTik firewall rule templates** -- Add a firewall rules template editor for MikroTik routers, with pre-built rule sets for: subscriber blocking (redirect to captive portal), address list management, and allowed resources. Support template variables like `{{ALLOWED_RESOURCES_ADDRESS_LIST}}` and `{{SYSTEM_IP_ADDRESS}}`
- [ ] **Mass router configuration push** -- Add a "Mass update all routers" action button that pushes the current firewall/QoS/shaping configuration to all connected MikroTik NAS devices, with progress tracking and error reporting
- [ ] **Allowed resources whitelist** -- Add a configurable list of IP addresses/hostnames that remain accessible to blocked subscribers (payment portal, captive portal, DNS)

---

## 8.25 Fair Usage Policy (FUP)

**What Splynx shows:** A FUP settings page with: customer additional field for custom reset dates, monthly reset schedule for prepaid daily limits, monthly reset for recurring limits (billing day), weekly limit reset day (Monday/Sunday), and a toggle to send notifications after applying FUP rules.

### Improvements

- [ ] **Fair Usage Policy configuration** -- Add a FUP settings page with: custom subscriber field for per-customer reset date override, monthly reset schedule for prepaid daily limits, monthly reset trigger for recurring limits (tied to billing day), weekly limit reset day (Monday or Sunday), and a toggle to send notifications to subscribers when FUP rules are applied
- [ ] **FUP notification templates** -- Link FUP notification settings to configurable templates so operators can customize the message subscribers receive when their usage reaches a threshold or limit
- [ ] **FUP threshold levels** -- Add support for multiple FUP threshold levels (e.g., 75% warning, 90% warning, 100% enforcement) with different actions and notifications at each level

---

## 8.26 NAS Types Configuration

**What Splynx shows:** A NAS Types list page showing 10 NAS types: MikroTik (with MikroTik API: Yes), Cisco, Ericsson, Linux PPPD, Ubiquiti, D-Link, Juniper, Cisco IOS, Cisco IOS XE, and netElastic (all with MikroTik API: No). Each has edit/delete actions and an "Add" button.

### Improvements

- [ ] **NAS type management** -- Build a NAS types configuration page listing supported NAS vendors/types with: name, MikroTik API support indicator (Yes/No), and edit/delete actions
- [ ] **NAS type capabilities** -- For each NAS type, configure supported capabilities: API management, SNMP monitoring, SSH access, CoA (Change of Authorization) support, and RADIUS accounting
- [ ] **Custom NAS type creation** -- Allow adding custom NAS types for vendor-specific equipment not in the default list, with configurable RADIUS attributes and API endpoints

---

## 8.27 IPv6 Configuration

**What Splynx shows:** An IPv6 settings page with: Auto-assign IPv6 to Customers (enable toggle, networks to auto-assign from, assign-to field: IPv6 network), Manual IPv6 Assignment (default prefix for network selection), and an "Update IPv6 on services for all customers" bulk action button.

### Improvements

- [ ] **IPv6 auto-assignment settings** -- Add IPv6 configuration with: enable auto-assignment toggle, network pool selection for auto-assignment, assignment target field selector (IPv6 network attribute on the service), and default prefix for manual selection
- [ ] **Bulk IPv6 update** -- Add a "Update IPv6 on all services" bulk action button that applies the current IPv6 auto-assignment configuration to all existing customer services, with a confirmation dialog and progress indicator
- [ ] **Dual-stack support settings** -- Add settings to control whether new services are provisioned with IPv4-only, IPv6-only, or dual-stack by default

---

## 8.28 IP Network Categories

**What Splynx shows:** An IP Network Categories page listing categories: Dev, Corp, Production. Simple table with ID, Category name, and edit/delete actions.

### Improvements

- [ ] **IP network category management** -- Add an IP network categories settings page for organizing IP pools into logical groups (e.g., Production, Development, Corporate, Management), with add/edit/delete actions and color coding for visual distinction in the IP management interface

---

## 8.29 Huawei OLT Board Support

**What Splynx shows:** A "Huawei supported boards" configuration page listing supported OLT board models with: ID, Name (e.g., H901GPLF), and Count of ports (8). Supports add/edit/delete.

### Improvements

- [ ] **OLT board model registry** -- Add a configurable registry of supported OLT board models (for Huawei and other vendors) with: board model name, number of ports, port type (GPON/EPON/XGS-PON), and vendor association
- [ ] **Auto-discovery board mapping** -- When auto-discovering OLT boards, match discovered board models against the registry to automatically set port counts and capabilities

---

## Priority Summary

### P0 -- Critical (Core system functionality gaps)
- [ ] Centralized settings hub page with search (8.1)
- [ ] Company information settings page (8.2)
- [ ] Email/SMTP configuration (8.9)
- [ ] Invoice auto-generation and billing automation settings (8.11)
- [ ] Recurring billing configuration with calendar visualization (8.12)
- [ ] Tax rate management (8.18)
- [ ] RADIUS configuration with reject IPs and session management (8.20)

### P1 -- High (Revenue and compliance impact)
- [ ] Notification template library with variable support (8.4)
- [ ] Payment methods management (8.13)
- [ ] Multi-wave billing reminder system (8.16)
- [ ] Billing notifications for prepaid/recurring (8.17)
- [ ] Transaction category management (8.14)
- [ ] Plan change refund and fee configuration (8.19)
- [ ] Data retention policy settings (8.10)
- [ ] Finance automation blocking rules (8.11)

### P2 -- Medium (Operational efficiency)
- [ ] Customer portal configuration with field permissions (8.8)
- [ ] Subscriber login/password generation settings (8.7)
- [ ] Per-reseller company info and email overrides (8.2, 8.9)
- [ ] Payment pairing/reconciliation rules (8.15)
- [ ] MikroTik API integration settings (8.24)
- [ ] CPE management configuration (8.22)
- [ ] FUP configuration (8.25)
- [ ] Monitoring configuration (8.23)
- [ ] NAS type management (8.26)

### P3 -- Low (Nice-to-have enhancements)
- [ ] System file manager with categorization (8.3)
- [ ] System preferences and security hardening (8.5)
- [ ] API configuration and key management (8.6)
- [ ] Accounting software integration mapping (8.14)
- [ ] NetFlow accounting configuration (8.21)
- [ ] IPv6 auto-assignment settings (8.27)
- [ ] IP network categories (8.28)
- [ ] Huawei OLT board registry (8.29)
- [ ] Mobile app integration settings (8.8)
- [ ] Document encryption settings (8.5)

---

*Document generated from review of 46 Splynx configuration screenshots. Each improvement item maps to observed capabilities in the Splynx platform and is proposed for implementation in DotMac Sub using the FastAPI/HTMX/Tailwind stack with the project's service-layer architecture.*
