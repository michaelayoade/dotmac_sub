# Section 2: Selfcare Portal & Messaging

## Source: Splynx ISP Management Platform
These screenshots document Splynx's tariff plan management (Internet plans, Recurring charges, One-time fees, Bundles) and messaging system (mass sending with advanced filters, rich-text editor, financial document attachments, and delivery history). The analysis below maps Splynx capabilities to feature improvements for DotMac Sub.

---

## 2.1 Internet Tariff Plans -- Admin Catalog View

**What Splynx has:**
- Dedicated "Tariff plans > Internet" section listing all internet service plans in a sortable, searchable table
- Columns: ID, Title, Price (in Naira), Download speed (Kbps), Upload speed (Kbps), Customers (count), Customers online (count), and Actions
- 57 plans visible ranging from 1 Mbps Fiber (21,500 NGN) to 700 Mbps Dedicated (1,304,411.72 NGN)
- Partner filter dropdown to view plans scoped to a specific reseller/partner
- Visible/Active toggle filters at the top right
- Per-plan action icons: a "change" icon (swap/transfer customers between plans) and a "statistics" icon (bar chart for plan analytics)
- Real-time customer count and online customer count per plan, providing instant visibility into plan popularity and active usage
- Plans include both capped (e.g., "20GB data" at 6,450 NGN) and unlimited tiers (e.g., "Unlimited Basic", "Unlimited Elite", "Unlimited Mega")
- Table search with configurable entries per page (100 shown)
- Pagination with total count display ("Showing 1 to 57 of 57 entries")

**Feature improvements for DotMac Sub:**

### Catalog List Enhancements
- [ ] **Live subscriber count per offer**: Display the current number of active subscribers on each catalog offer directly in the offers list table, eliminating the need to navigate into each offer to see adoption
- [ ] **Online subscriber count per offer**: Show how many subscribers on each plan are currently online (via RADIUS session data), giving operators instant insight into active usage per tier
- [ ] **Download/Upload speed columns in offer list**: Add sortable speed columns (download Mbps, upload Mbps) to the catalog offers table so operators can scan bandwidth tiers at a glance without opening each offer
- [ ] **Plan statistics action button**: Add a per-offer action icon that opens a statistics view showing subscriber adoption over time, churn rate, revenue contribution, and online vs offline breakdown for that specific plan
- [ ] **Bulk plan migration tool**: Add a "transfer subscribers" action per offer that allows operators to move all subscribers from one plan to another (e.g., during plan consolidation or price changes), with preview of affected subscribers and confirmation step
- [ ] **Partner/reseller filter on catalog**: Add a reseller filter dropdown on the catalog offers list page so multi-tenant operators can view offers scoped to a specific partner/reseller organization
- [ ] **Visible/Active status toggle filters**: Add quick-toggle filters (Visible, Active/Inactive/Archived) at the top of the catalog list to show/hide plans by their portal visibility and lifecycle status

### Capped vs Unlimited Plan Support
- [ ] **Data cap plan type**: Enhance `CatalogOffer` to explicitly support capped data plans (e.g., "20GB data") with a `data_cap_gb` field, distinct from unlimited plans that have no cap but may have speed tiers
- [ ] **Plan naming convention enforcement**: Add admin guidance or validation suggesting consistent plan naming (e.g., "{Speed} Mbps Fiber", "Unlimited {Tier}") to prevent the inconsistency visible in Splynx (e.g., "unlimited 1.5" vs "Unlimited Basic")

---

## 2.2 Recurring Tariff Plans

**What Splynx has:**
- Separate "Tariff plans > Recurring" section for non-internet recurring charges
- Table columns: ID, Title, Price, Customers, Actions (statistics chart icon)
- 13 recurring plans including:
  - Infrastructure: "Fiber Last Mile" (130,000 NGN), "45mbps Leased Line" (130,000 NGN)
  - Unlimited tiers: "Unlimited 10", "unlimited 1.5", "unlimited 3", "Unlimited midi 5MBPS"
  - Static IP add-ons: "/32 IP" (2,687.50 NGN, 41 customers), "/30 IP" (10,750 NGN, 29 customers), "/29 IP" (21,500 NGN, 14 customers), "/28 IP" (37,625 NGN)
  - Device services: "Device Replacement" (26,875 NGN), "Device Replacement (4)" (14,781.25 NGN)
- Customer count per recurring plan shows adoption (e.g., 41 subscribers with /32 IP, 10 with unlimited 3)
- Partner filter and Visible/Active toggles identical to Internet plans

**Feature improvements for DotMac Sub:**

### Recurring Charge Catalog
- [ ] **Separate recurring charges view**: Create a dedicated "Recurring Charges" sub-tab under Catalog that lists non-internet recurring services (static IP blocks, leased lines, device rentals, last-mile charges) separately from internet plans
- [ ] **Static IP add-on tiers**: Pre-define standard IP allocation add-ons (/32, /30, /29, /28) with automatic pricing and IPAM integration, so operators can assign IP blocks as recurring charges linked to the IP address management system
- [ ] **Leased line / dedicated circuit plans**: Support a "dedicated" plan type with committed information rate (CIR) guarantees, separate from shared residential plans, with distinct billing and SLA terms
- [ ] **Device replacement as recurring service**: Allow "device rental" or "device replacement" to be configured as a recurring charge add-on that auto-bills monthly, separate from one-time installation fees
- [ ] **Add-on subscriber count**: Show the count of active subscribers using each add-on/recurring charge in the catalog list, matching the visibility Splynx provides

---

## 2.3 One-Time Tariff Plans

**What Splynx has:**
- Separate "Tariff plans > One-time" section for non-recurring charges
- Table columns: ID, Title, Enabled (Yes/No badge), Price, Actions
- 8 one-time charges defined:
  - "Air Fibre Installation Cost" (30,000 NGN)
  - "Call down support" (5,000 NGN)
  - "Faulty Device Replacement" (0.00 NGN -- no charge)
  - "Dropcable Rerun" (0.00 NGN -- no charge)
  - "Ground Fibre Installation Cost" (150,000 NGN)
  - "Relocations" (0.00 NGN -- no charge, likely billed separately)
  - "Referral Bonus" (10,000 NGN -- credited to referrer)
  - "Device Replacement" (50,000 NGN)
- Enabled toggle per charge (all show "Yes" green badge)
- Partner and Visible/Active filters consistent with other plan types

**Feature improvements for DotMac Sub:**

### One-Time Fee Catalog
- [ ] **Dedicated one-time fees view**: Create a "One-Time Fees" sub-tab under Catalog listing all non-recurring charges (installation, support calls, relocations, device replacements) with enabled/disabled toggle
- [ ] **Enabled/disabled toggle per fee**: Add a quick-toggle "Enabled" column (green/red badge) allowing operators to temporarily disable a one-time fee without deleting it from the catalog
- [ ] **Installation fee variants**: Support multiple installation fee types (aerial fiber vs ground fiber, with different pricing) as distinct one-time catalog items selectable during service provisioning
- [ ] **Referral bonus as catalog item**: Allow a "Referral Bonus" one-time credit item in the catalog that can be automatically applied when a referral code is validated, integrating with the billing credit system
- [ ] **Zero-cost service items**: Support 0.00 priced one-time items for tracking purposes (e.g., "Faulty Device Replacement" at no charge still creates a service order and audit trail even though no invoice is generated)
- [ ] **One-time fee application from service order**: When creating a service order (provisioning), allow operators to select applicable one-time fees from the catalog to automatically generate an invoice line item alongside the recurring subscription charge

---

## 2.4 Bundle Plans

**What Splynx has:**
- "Tariff plans > Bundles" section for combined service packages
- Table columns: ID, Title, Price, Customers, Actions
- Currently empty ("No data available in table") indicating the feature exists but is not yet used by this operator
- The structure supports grouping multiple services (internet + voice + IPTV) into a single bundled offering with a combined price
- Partner and Visible/Active filters available

**Feature improvements for DotMac Sub:**

### Service Bundles
- [ ] **Bundle catalog type**: Add a "Bundle" offer type that groups multiple catalog offers (e.g., internet plan + static IP add-on + router rental) into a single purchasable package with a combined or discounted price
- [ ] **Bundle pricing modes**: Support bundle pricing as either (a) sum of component prices with a percentage discount, (b) a flat bundle price overriding component prices, or (c) "buy X get Y free" where purchasing one component unlocks another at no cost
- [ ] **Bundle component management**: Admin UI for composing bundles by selecting component offers, setting per-component quantities, and defining which components are mandatory vs optional
- [ ] **Customer portal bundle display**: Show available bundles on the customer self-service portal with a comparison view highlighting the savings vs purchasing components individually
- [ ] **Bundle subscription tracking**: When a subscriber purchases a bundle, create linked subscriptions for each component but track them under a single bundle identifier for billing and lifecycle management

---

## 2.5 Mass Message Sending -- Recipient Targeting

**What Splynx has:**
- "Messages > Mass sending > Create" form with extensive recipient targeting filters
- Recipient type selector: "Customer" (dropdown, likely also supports "Lead", "Admin")
- Targeting filters include:
  - Portal login (specific customer account)
  - Status (active, inactive, suspended, etc.)
  - Labels (tag-based filtering, typeahead: "Start typing label name")
  - Full name, Email, Billing email, Phone number (text search filters)
  - Category (dropdown: "Select category")
  - Location (dropdown)
  - Network sites (dropdown)
  - Access devices (dropdown -- NAS/OLT selection)
  - Billing type (dropdown)
  - Partner (reseller/partner filter)
  - Tariff plans (dropdown -- send to subscribers on specific plans)
  - Service type (dropdown: "Select service type")
  - Base Station, Project Office (text fields for location-based targeting)
  - Custom fields: zoho_id, vat_id, BUILDING TYPE
- "Send to" channel selector: Email (dropdown, likely also SMS)
- "Send to billing email" toggle (separate from primary email)
- Subject line field

**Feature improvements for DotMac Sub:**

### Mass Messaging -- Recipient Targeting
- [ ] **Mass message composition page**: Create an admin page at `/admin/notifications/mass-send` with a form for composing and sending bulk messages to filtered subscriber groups
- [ ] **Subscriber status filter**: Filter recipients by subscriber status (active, suspended, canceled, pending) so operators can target only active customers or specifically reach suspended ones with reactivation offers
- [ ] **Plan/offer filter**: Filter recipients by their current catalog offer/tariff plan, enabling targeted communications like "upgrade available" messages to subscribers on lower-tier plans
- [ ] **Location/area filter**: Filter recipients by service address location, POP site, or region zone for geographically targeted messages (e.g., planned maintenance in a specific area)
- [ ] **NAS/access device filter**: Filter recipients by the NAS device or OLT serving their connection, enabling precise targeting for device-specific maintenance windows or outage notifications
- [ ] **Billing type filter**: Filter by billing mode (prepaid/postpaid) for billing-specific communications (e.g., payment reminders for postpaid, top-up reminders for prepaid)
- [ ] **Reseller/partner filter**: Scope mass messages to subscribers belonging to a specific reseller/partner organization for partner-specific communications
- [ ] **Label/tag-based targeting**: Filter recipients using subscriber tags/labels for custom segmentation (e.g., "VIP", "beta-tester", "referral-program")
- [ ] **Service type filter**: Filter by access type (fiber, fixed wireless, DSL) for technology-specific announcements
- [ ] **Multi-channel send**: Support sending the same message via Email, SMS, or both simultaneously, with a channel selector and a "send to billing email" toggle for financial communications
- [ ] **Recipient preview and count**: Before sending, display a preview showing the total number of matching recipients and a sample list, so operators can verify their filters before committing

---

## 2.6 Mass Message Sending -- Composition & Financial Attachments

**What Splynx has:**
- Rich-text message editor with formatting toolbar: Bold, Italic, Underline, Ordered list, Unordered list, Link, Blockquote, Table, Image, Clear formatting, Code/HTML view
- Message template system: "Templates" dropdown with "Load" button to populate the message body from a pre-defined template
- "Attachments" section with an "Upload" button for adding file attachments
- "Attach financial documents" section with toggles for:
  - Invoices (with date range picker: "24/02/2026 - 24/02/2026" and Status/Payment type filter: "Any")
  - Credit notes (with date range and status filter)
  - Proforma invoice (with date range and status filter)
  - Payments (with date range and status/payment type filter)
  - Toggle: "Send the message if the financial document exists" (conditional send -- only send to customers who actually have matching documents)
- Action buttons: "Preview", "Send as test", "Reset", "Send"
- The conditional send toggle is particularly powerful: it ensures customers only receive financial document emails if they actually have matching invoices/payments, preventing irrelevant emails

**Feature improvements for DotMac Sub:**

### Message Composition
- [ ] **Rich-text email editor**: Integrate a rich-text editor (e.g., TipTap or TinyMCE) for mass message composition, supporting bold, italic, lists, links, tables, images, and raw HTML editing
- [ ] **Message template loader**: Add a "Load template" dropdown that populates the subject and body from existing `NotificationTemplate` records, with a "Load" button to apply the selected template
- [ ] **File attachment support**: Allow uploading file attachments (PDF, images) to mass messages, stored via the existing `file_storage` service and attached to outgoing emails
- [ ] **Financial document auto-attachment**: Allow toggling automatic attachment of invoices, credit notes, proforma invoices, or payment receipts to mass emails, with date range and status filters to select which documents to include
- [ ] **Conditional send on document existence**: Add a "Only send if financial document exists" toggle that skips recipients who have no matching financial documents in the selected date range, preventing irrelevant email sends
- [ ] **Preview before send**: Add a "Preview" button that renders the message with a sample recipient's data (name, account number) so operators can verify the final output before sending
- [ ] **Test send**: Add a "Send as test" button that sends the composed message to the logged-in admin's email address for review before mass distribution
- [ ] **Template variable placeholders**: Support placeholder variables in message templates (e.g., `{{customer_name}}`, `{{account_number}}`, `{{balance}}`, `{{plan_name}}`) that auto-resolve per recipient during send

---

## 2.7 Payment Totals Summary (Finance Context)

**What Splynx has:**
- A "Totals" summary table showing payment collection aggregated by payment provider/type
- Columns: Type (colored badge), Amount (count of transactions), Total (monetary sum)
- Payment types visible:
  - Cash CBD: 1 transaction, 37,100.00 NGN
  - Zenith 461 Bank: 315 transactions, 15,919,598.20 NGN
  - Paystack: 578 transactions, 16,122,153.19 NGN
  - QuickBooks: 0 transactions, 0.00 NGN
  - Zenith 523 Bank: 5 transactions, 1,347,700.00 NGN
  - Dotmac USD: 0 transactions, 0.00 NGN
  - Flutterwave: 0 transactions, 0.00 NGN
  - UBA: 0 transactions, 0.00 NGN
  - Remita: 0 transactions, 0.00 NGN
  - **Total: 899 transactions, 33,426,551.39 NGN**
- Color-coded payment type badges (blue labels) for quick identification
- Shows both transaction count and monetary total per provider

**Feature improvements for DotMac Sub:**

### Payment Provider Summary
- [ ] **Payment totals by provider widget**: Add a summary table to the billing overview dashboard showing transaction count and total amount grouped by payment provider/method (Paystack, Flutterwave, bank transfer, cash, etc.)
- [ ] **Payment provider badges**: Use color-coded badges for each payment provider type in summary tables and payment lists for quick visual identification
- [ ] **Period-filtered payment summary**: Allow filtering the payment provider summary by date range (daily, weekly, monthly, custom) to track collection trends per channel
- [ ] **Provider performance comparison**: Show percentage of total collections per provider alongside absolute amounts, helping operators identify which payment channels are most effective

---

## 2.8 Mass Message History & Delivery Tracking

**What Splynx has:**
- "Messages > Mass sending > History" page showing all sent mass messages
- Table columns: ID, Send to (channel), Subject, Created (timestamp), Status (green "Sent" badges), Customer login (account identifier), Actions
- Filters: Status dropdown ("Any"), Period date range picker ("01/02/2026 - 28/02/2026"), Table search
- Sample data shows:
  - Message ID range: 355677-355694+
  - All sent via "Email" channel
  - Subjects: "Service Outage" (one-off), "Dotmac Technologies - Subscription Notification" (bulk)
  - Timestamps: "01/02/2026 04:26:52" through "01/02/2026 10:00:12"
  - All statuses show green "Sent" badges
  - Customer login IDs: "100000033 (+129)" indicating one message sent to 129+ recipients, individual customer login IDs for per-recipient tracking
- Action icons per message: view, resend, delete
- Configurable entries per page (100 shown)

**Feature improvements for DotMac Sub:**

### Message Delivery History
- [ ] **Mass message history page**: Create an admin page at `/admin/notifications/mass-send/history` showing a table of all mass messages sent, with columns for ID, channel, subject, created timestamp, status, recipient count, and actions
- [ ] **Delivery status per recipient**: Track and display delivery status (sent, delivered, failed, bounced) per individual recipient within a mass message campaign, not just the aggregate status
- [ ] **Status filter on history**: Add a status dropdown filter (All, Sent, Failed, Pending) and a date range picker to filter the mass message history
- [ ] **Recipient count display**: Show the total number of recipients targeted by each mass message in the history list (e.g., "129 recipients") with a link to view the full recipient list
- [ ] **Resend failed messages**: Add a "Resend" action per mass message that re-queues delivery only to recipients whose original delivery failed or bounced
- [ ] **Message view action**: Add a "View" action that opens the full message content, recipient list, and per-recipient delivery status in a detail page
- [ ] **Export message history**: Allow exporting the mass message history (CSV/Excel) with delivery statistics for compliance and reporting
- [ ] **Campaign analytics**: Show per-campaign metrics: total sent, delivered, failed, bounce rate, and delivery time distribution

---

## 2.9 Customer Self-Service Portal Enhancements

**What Splynx has (inferred from tariff plan structure):**
- Tariff plans marked as visible/active are shown on the customer selfcare portal
- Customers can view available plans and request upgrades/downgrades
- Plan details include speed, price, and included services
- Bundle availability on the portal for combined service packages

**Feature improvements for DotMac Sub:**

### Customer Portal -- Plan Management
- [ ] **Plan comparison page**: Add a customer-facing page showing all available plans in a side-by-side comparison grid with speed, price, data cap, and included features, allowing customers to evaluate upgrade options
- [ ] **Self-service plan change request**: Allow customers to request a plan change (upgrade or downgrade) from the portal, which creates a service order for admin approval or auto-provisions if configured
- [ ] **Plan change cost preview**: Before confirming a plan change, show the customer the prorated cost difference, new monthly amount, and effective date
- [ ] **Current plan usage dashboard**: Show customers their current plan details alongside real-time usage data (bandwidth consumed, data cap remaining for capped plans, session uptime)
- [ ] **Plan recommendation engine**: Based on a customer's historical usage patterns, suggest optimal plans (e.g., "You regularly exceed your 20GB cap -- consider upgrading to Unlimited Basic for 18,812.50 NGN/month")

### Customer Portal -- Notifications
- [ ] **Customer notification inbox**: Add an in-app notification center in the customer portal showing messages sent by the ISP (service outage alerts, subscription notifications, billing reminders)
- [ ] **Notification preferences**: Allow customers to manage their notification preferences (opt-in/opt-out per channel: email, SMS, in-app) from their profile settings
- [ ] **Service outage alerts**: Display active service outage notifications prominently on the customer dashboard when their area or NAS device is affected

---

## Priority Summary

### P0 -- Critical (High business impact, needed for operational parity)
| Improvement | Section | Rationale |
|-------------|---------|-----------|
| Mass message composition page | 2.5 | Operators currently lack a way to bulk-communicate with subscriber segments; essential for outage notifications, billing reminders, and marketing |
| Subscriber status/plan/location filters for mass send | 2.5 | Without granular targeting, mass messages are either all-or-nothing, leading to irrelevant communications |
| Message delivery history page | 2.8 | No audit trail for sent communications creates compliance risk and makes it impossible to verify delivery |
| Live subscriber count per offer | 2.1 | Critical catalog management metric; operators need to see plan adoption without running separate reports |
| Financial document auto-attachment | 2.6 | Eliminates manual invoice distribution; major time savings for monthly billing cycles |

### P1 -- High (Significant operational improvement)
| Improvement | Section | Rationale |
|-------------|---------|-----------|
| Rich-text email editor | 2.6 | Plain-text-only messages look unprofessional; rich formatting is expected for ISP communications |
| Message template loader | 2.6 | Reduces composition time and ensures consistency across recurring communications |
| Dedicated one-time fees view | 2.3 | One-time charges (installations, relocations) are fundamental ISP operations but not distinctly managed today |
| Separate recurring charges view | 2.2 | Static IP blocks and leased lines need separate catalog management from internet plans |
| Payment totals by provider widget | 2.7 | Financial oversight requires per-provider collection visibility |
| Preview and test send | 2.6 | Prevents embarrassing mistakes in mass communications |
| Delivery status per recipient | 2.8 | Required to identify and remediate failed deliveries |
| Plan comparison page (customer portal) | 2.9 | Self-service reduces support calls and increases upgrade conversions |

### P2 -- Medium (Quality of life, enhanced experience)
| Improvement | Section | Rationale |
|-------------|---------|-----------|
| Bundle catalog type | 2.4 | Enables competitive multi-service packages but not immediately needed if operator offers single services |
| Online subscriber count per offer | 2.1 | Useful network insight but not blocking for daily operations |
| Plan statistics action button | 2.1 | Valuable analytics but can use existing reports as workaround |
| Bulk plan migration tool | 2.1 | Major time saver during plan consolidation but infrequent operation |
| Conditional send on document existence | 2.6 | Prevents irrelevant emails but can be worked around with manual filtering |
| Resend failed messages | 2.8 | Improves delivery reliability but volume of failures is typically low |
| Self-service plan change request | 2.9 | Reduces support burden but requires careful integration with provisioning |
| Customer notification inbox | 2.9 | Enhances customer experience but email covers most cases initially |

### P3 -- Low (Nice to have, future consideration)
| Improvement | Section | Rationale |
|-------------|---------|-----------|
| Plan naming convention enforcement | 2.1 | Organizational hygiene; low urgency |
| Data cap plan type field | 2.1 | Can be managed via usage allowance model already in place |
| Referral bonus catalog item | 2.3 | Useful for growth programs but not core ISP operations |
| Bundle pricing modes | 2.4 | Complex feature; needed only after bundle type is implemented |
| Campaign analytics | 2.8 | Advanced reporting; basic history covers initial needs |
| Plan recommendation engine | 2.9 | AI-driven feature; high value but high implementation effort |
| Export message history | 2.8 | Compliance value but low day-to-day urgency |
| Provider performance comparison | 2.7 | Advanced financial analytics |
