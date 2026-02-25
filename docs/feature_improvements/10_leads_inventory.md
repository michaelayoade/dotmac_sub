# Section 10: Leads/CRM & Inventory Management

## Source: Splynx ISP Management Platform

---

## Overview

This document analyzes Splynx's Leads/CRM module and Inventory management features based on 15 screenshots covering lead pipeline configuration, signup widgets, lead field customization, notification settings, quote/finance management, lead conversion settings, IMAP integration, and inventory stock/category management. These are capabilities DotMac Sub currently lacks and would significantly enhance its pre-sales and equipment tracking workflows.

---

## 10.1 Lead Pipeline Configuration

### Screenshot Analysis
- **Leads Pipeline** (`133100.png`): Shows a configurable pipeline with 6 stages: New enquiry (New), Contacted (In Progress), Qualification (In Progress), Activation (In Progress), Won (Won), Lost (Lost). Each stage has an ID, Status label, associated Stage (mapped to New/In Progress/Won/Lost), reorderable position (up/down arrows), and edit/delete actions. An "Add" button allows creating new pipeline stages.
- **Preferences - Associate Quote Stages with Lead Pipeline Statuses** (`132633.png`): Maps each pipeline stage to a lead status (New, In Progress, Won, Lost). Five stages mapped: New -> New, Sent -> In Progress, On review -> In Progress, Accepted -> Won, Denied -> Lost. This links the quoting workflow to the sales pipeline.

### Proposed Improvements for DotMac Sub

- [ ] **Create a `Lead` model** with fields: id (UUID), full_name, email, phone_number, source, partner/reseller, location, city, street, zip_code, geo_data, score (numeric lead scoring), billing_email, status (enum), pipeline_stage_id (FK), owner_id (FK to User), organization_id (FK), notes, created_at, updated_at
- [ ] **Create a `LeadPipelineStage` model** with fields: id (UUID), name (e.g., "New Enquiry", "Contacted"), stage_type (enum: new, in_progress, won, lost), position (integer for ordering), organization_id, is_default (boolean), created_at, updated_at
- [ ] **Build pipeline stage CRUD service** (`app/services/lead_pipeline.py`) with create, list (ordered by position), update, delete, and reorder operations
- [ ] **Build lead pipeline configuration UI** at `/admin/leads/pipeline` showing a table of stages with drag-to-reorder or up/down position controls, edit/delete actions, and an "Add Stage" button
- [ ] **Implement default pipeline seeding** with 6 stages on organization creation: New Enquiry (new), Contacted (in_progress), Qualification (in_progress), Activation (in_progress), Won (won), Lost (lost)
- [ ] **Map pipeline stages to lead statuses** so that moving a lead to a "Won" stage automatically sets the lead's status to "won" and moving to "Lost" sets it to "lost"
- [ ] **Add pipeline stage association to quote workflow** so that quote acceptance/denial automatically advances the lead through the pipeline (e.g., quote accepted -> Won stage)

---

## 10.2 Lead Fields Configuration

### Screenshot Analysis
- **Leads Fields** (`132840.png`, `132853.png`): Shows 13 configurable lead fields, all typed as "Main": Full name, Phone number, Email, Source, Partner, Location, City, Street, ZIP Code, Geo data, Score, Date added, Billing email. Each field has a Type (Main), Name, Position (reorderable with up/down arrows), and a delete action. A "Default fields" section sets the number of fields shown by default (10). A "+" button adds new custom fields. Table search is available.

### Proposed Improvements for DotMac Sub

- [ ] **Create a `LeadFieldConfig` model** for customizable lead fields: id, field_name, field_label, field_type (enum: text, email, phone, select, number, date, geo), is_required (boolean), is_visible (boolean), position (integer), category (enum: main, additional), organization_id, created_at, updated_at
- [ ] **Build lead field configuration UI** at `/admin/leads/settings/fields` with a table listing all configured fields, their type, position controls, visibility toggles, and delete actions
- [ ] **Support custom fields** beyond the default set -- allow admins to add organization-specific fields (e.g., "Referrer", "Building Name", "Apartment Number") with configurable types
- [ ] **Implement field ordering** with drag-to-reorder or up/down arrow controls that persist position in the database
- [ ] **Add a "Default fields shown" setting** to control how many fields are visible by default on the lead list view before requiring column expansion
- [ ] **Seed default lead fields** on organization creation: Full name, Phone number, Email, Source, Location, City, Street, ZIP Code, Geo data, Score, Date added, Billing email

---

## 10.3 Signup Widget (Public Lead Capture Form)

### Screenshot Analysis
- **Signup Widget** (`132724.png`, `132751.png`, `132802.png`): A configurable public-facing lead capture form with settings including: Form title ("New Leads"), Form send button text ("Subscribe"), Thank you message ("Thanks for subscribing an agent to get back to you shortly."), Use HTTPS toggle, Partner assignment (Main), Owner assignment (splynx user), Location (Abuja), Pipeline status (New enquiry), Tariffs selection (7 of 70 selected), Choosing tariff is required toggle, Include VAT in tariff price toggle, Show Terms & Conditions checkbox toggle. Below are notification settings for admins and a result code section with embeddable HTML/JavaScript widget code. Form fields section shows 6 fields: First name (Main, required), Last name (Main, required), Email (Main, enabled), Phone number (Main, enabled), Referrer (Additional, enabled), City (Main, enabled) -- each with type, position controls, required toggle, and actions.

### Proposed Improvements for DotMac Sub

- [ ] **Create a `LeadSignupWidget` model** with fields: id, title, button_text, thank_you_message, use_https, default_partner_id, default_owner_id, default_location, default_pipeline_stage_id, show_tariff_selection, tariff_selection_required, include_vat_in_price, show_terms_checkbox, organization_id, is_active, created_at, updated_at
- [ ] **Create a `LeadSignupWidgetField` model** linking fields to widgets: id, widget_id (FK), field_config_id (FK), position, is_required, is_visible
- [ ] **Build a public lead capture endpoint** at `/public/leads/signup/<widget_id>` that renders a standalone, embeddable form styled with the ISP's branding
- [ ] **Generate embeddable widget code** (iframe or JavaScript snippet) that ISPs can paste into their external websites to capture leads directly into DotMac Sub
- [ ] **Build widget configuration UI** at `/admin/leads/widgets` for managing multiple signup widgets with different configurations per location, partner, or campaign
- [ ] **Allow tariff/offer selection on the signup form** so prospects can choose their desired service plan during lead submission, pre-populating the subscription intent
- [ ] **Auto-assign leads** to the configured pipeline stage, owner, partner, and location based on widget settings
- [ ] **Display a configurable thank-you message** after form submission and optionally redirect to a custom URL
- [ ] **Support Terms & Conditions checkbox** with a link to the organization's legal documents (integrating with existing DotMac Sub legal document management)

---

## 10.4 Lead Notifications

### Screenshot Analysis
- **Notifications** (`132930.png`, `132941.png`): Three notification categories configured: (1) **Due notifications** -- enable/disable for leads with statuses due by configurable days, send at a specific time (09:00), with a selectable template. (2) **Reminder notifications** -- template selection for CRM reminders. (3) **Quote notifications** -- auto-send after creating a quote, configurable send channel (Email), Email Template ("Quote notification"), SMS Template, Email BCC address, delay in sending (3 hours), notification days (Monday-Friday), notification hours (9 of 24 selected). (4) **Notifications for admin** -- send to Email, notify via email selector, Email Template on Accept ("On accept mail"), Email Template on Reject ("On reject mail").

### Proposed Improvements for DotMac Sub

- [ ] **Create a `LeadNotificationConfig` model** for per-organization notification settings: id, organization_id, due_notifications_enabled, due_notification_time, due_notification_template_id, reminder_template_id, quote_auto_notify, quote_notify_channel (enum: email, sms, both), quote_email_template_id, quote_sms_template_id, quote_email_bcc, quote_send_delay_hours, notification_days (JSON array), notification_hours (JSON array), admin_notify_channel, admin_accept_template_id, admin_reject_template_id
- [ ] **Implement lead due date notifications** via a Celery task (`app/tasks/leads.py`) that runs daily, checks for leads with upcoming due dates, and sends notifications at the configured time
- [ ] **Implement quote notification auto-sending** that triggers after a quote is created, respecting the configured delay, allowed days, and allowed hours before dispatching
- [ ] **Add admin notification on lead accept/reject** so that when a prospect accepts or rejects a quote, the assigned admin/owner receives an email with the appropriate template
- [ ] **Implement CRM reminder notifications** that alert lead owners about follow-up tasks on leads that have been idle for a configurable period
- [ ] **Integrate with existing DotMac Sub notification system** by creating new EventType values: LEAD_CREATED, LEAD_UPDATED, LEAD_STAGE_CHANGED, QUOTE_CREATED, QUOTE_ACCEPTED, QUOTE_REJECTED, LEAD_CONVERTED
- [ ] **Support notification scheduling** with configurable business days and business hours to prevent sending lead-related notifications outside working hours

---

## 10.5 Quote & Finance Settings

### Screenshot Analysis
- **Finance** (`132654.png`): Quote settings section with two fields: Expiration date (set to 10 days -- "Number of days before the quote expires") and Quote number pattern (set to `{year}{next|6}` with available variables: {lead_id}, {partner_id}, {location_id}, {rand_string}, {rand_number}, {year}, {month}, {day}, {next}).

### Proposed Improvements for DotMac Sub

- [ ] **Create a `Quote` model** with fields: id (UUID), quote_number (string, auto-generated), lead_id (FK), organization_id, status (enum: draft, sent, on_review, accepted, denied, expired), items (JSON or related QuoteItem table), subtotal, tax_amount, total, currency, expiration_date, created_by (FK to User), notes, created_at, updated_at
- [ ] **Create a `QuoteItem` model** with fields: id, quote_id (FK), description, offer_id (FK, optional -- link to catalog offer), quantity, unit_price, discount_percent, line_total
- [ ] **Implement configurable quote numbering** with a pattern system supporting variables: {year}, {month}, {day}, {next} (auto-incrementing sequence), {lead_id}, {partner_id}, {location_id}, {rand_string}, {rand_number}
- [ ] **Add quote expiration setting** at the organization level (default: 10 days), with automatic status transition to "expired" via a Celery task that runs daily
- [ ] **Build quote CRUD service** (`app/services/quotes.py`) with create, list, get, update, send, accept, reject, and expire operations
- [ ] **Build quote management UI** at `/admin/leads/<lead_id>/quotes` for creating and managing quotes associated with a lead
- [ ] **Generate PDF quotes** using the existing invoice PDF generation pattern, branded with the organization's logo and details
- [ ] **Link quotes to catalog offers** so that line items can reference existing service plans, ensuring pricing consistency

---

## 10.6 Quote Accept Settings (OTP Verification)

### Screenshot Analysis
- **Preferences - Quote Accept Settings** (`132633.png` lower section): Configures how prospects verify their identity when accepting/rejecting quotes. Fields: Email template for OTP ("Quote OTP PIN"), SMS text for OTP (templated: "Your one-time password (OTP) is: {{ code }} / The OTP will be valid for: {{ code_valid_until }}"), OTP validity (1 hour).

### Proposed Improvements for DotMac Sub

- [ ] **Implement OTP-based quote verification** so that when a prospect clicks accept/reject on a quote, they must verify their identity via a one-time password sent to their email or phone
- [ ] **Create a `QuoteOTP` model** with fields: id, quote_id, code (hashed), channel (enum: email, sms), expires_at, is_used, created_at
- [ ] **Add OTP settings to organization configuration** with configurable validity period (default: 1 hour), email template, and SMS text template
- [ ] **Build a public quote review page** at `/public/quotes/<token>` where prospects can view the quote details and accept or reject with OTP verification
- [ ] **Integrate OTP delivery** with existing DotMac Sub email and SMS services

---

## 10.7 IMAP Integration

### Screenshot Analysis
- **IMAP** (`132818.png`): Two settings sections: (1) **Main settings** -- Enable IMAP processing toggle (enabled). (2) **Email tracking** -- Track opens and clicks toggle (enabled). A Save button at the bottom.

### Proposed Improvements for DotMac Sub

- [ ] **Implement IMAP email integration for leads** that monitors a configured mailbox and automatically creates or updates leads based on incoming emails
- [ ] **Add IMAP connection settings** to the lead configuration: IMAP server, port, username, password (encrypted via credential_crypto), SSL toggle, folder to monitor
- [ ] **Implement email open/click tracking** by embedding tracking pixels in outbound lead emails and tracking link click-throughs
- [ ] **Create an `EmailTracking` model** with fields: id, lead_id, email_message_id, event_type (enum: sent, delivered, opened, clicked, bounced), event_data (JSON), tracked_at
- [ ] **Build email tracking dashboard** showing open rates, click rates, and bounce rates for lead-related communications
- [ ] **Auto-associate inbound emails with leads** by matching sender email address to existing lead records

---

## 10.8 Lead Conversion Settings

### Screenshot Analysis
- **Lead Convert Settings** (`132958.png`): Two sections: (1) **Lead status conversion type** -- three toggle options for what happens when a lead is converted: "Active (without invoice)" (disabled), "Active (with invoice)" (enabled), "New (with proforma invoice)" (enabled, marked as default and cannot be disabled). (2) **Conversion settings** -- Default customer type for lead conversion (dropdown: "Create active customer" with a reset button), Default won status (dropdown: "Won" -- "Default won status that will be set for lead after conversion").

### Proposed Improvements for DotMac Sub

- [ ] **Implement lead-to-subscriber conversion** that creates a Subscriber record from a Lead record, transferring all relevant data (name, email, phone, address, location, selected tariff/offer)
- [ ] **Support multiple conversion types**: (a) Convert to active subscriber without invoice, (b) Convert to active subscriber with invoice auto-generated, (c) Convert to new subscriber with proforma invoice
- [ ] **Add conversion settings to organization configuration**: default conversion type, default subscriber status after conversion, default "won" pipeline status
- [ ] **Automatically set lead pipeline stage to "Won"** upon successful conversion
- [ ] **Create a service order on conversion** that triggers the existing provisioning workflow for the new subscriber's selected service plan
- [ ] **Preserve lead history** after conversion by linking the converted subscriber back to the original lead record for CRM reporting
- [ ] **Build a conversion confirmation dialog** at `/admin/leads/<lead_id>/convert` that shows a summary of what will be created (subscriber, subscription, invoice) and allows the admin to review before confirming
- [ ] **Support bulk lead conversion** for scenarios where multiple qualified leads need to be converted simultaneously (e.g., new estate rollout)

---

## 10.9 Inventory -- Stock Locations

### Screenshot Analysis
- **Stock Locations** (`133914.png`): A table listing warehouse/stock locations with columns: ID, Name, Type, Actions (edit/delete). Two locations shown: "Main" (Warehouse) and "Wuse 2 Branch" (Warehouse). Add (+) and refresh buttons at top. Table search available.

### Proposed Improvements for DotMac Sub

- [ ] **Create a `StockLocation` model** with fields: id (UUID), name, location_type (enum: warehouse, branch, field_office, vehicle), address, city, state, geo_coordinates, contact_person, contact_phone, organization_id, is_active, created_at, updated_at
- [ ] **Build stock location CRUD service** (`app/services/inventory.py`) with create, list, get, update, and delete (soft-delete) operations
- [ ] **Build stock location management UI** at `/admin/inventory/locations` with a table showing all locations, type badges, and edit/delete actions
- [ ] **Support multiple location types** beyond just "Warehouse" -- include Branch, Field Office, Technician Vehicle for tracking equipment in transit or assigned to field teams
- [ ] **Link stock locations to POP sites** by allowing a stock location to be associated with an existing network POP site for geographic context

---

## 10.10 Inventory -- Equipment Categories

### Screenshot Analysis
- **Categories** (`133952.png`): A table listing inventory item categories with columns: ID, Title, Actions (edit/delete). Seven categories shown: Router, Switch, Access Point, UPS, Server, CPE, Other. Add (+) and refresh buttons at top. Table search available.

### Proposed Improvements for DotMac Sub

- [ ] **Create an `InventoryCategory` model** with fields: id (UUID), title, description, parent_category_id (FK, nullable -- for hierarchical categories), organization_id, is_active, created_at, updated_at
- [ ] **Build inventory category CRUD service** with standard operations and support for hierarchical nesting (e.g., CPE -> ONU, CPE -> Router)
- [ ] **Build category management UI** at `/admin/inventory/categories` with a table listing categories, edit/delete actions, and an "Add Category" button
- [ ] **Seed default ISP categories** on organization creation: Router, Switch, Access Point, UPS, Server, CPE, ONU/ONT, Fiber Cable, Splitter, Patch Panel, SFP Module, Media Converter, Other
- [ ] **Link inventory categories to network device types** so that when a CPE is assigned from inventory to a subscriber, the corresponding network device record is created or updated

---

## 10.11 Inventory -- Low Stock Notifications

### Screenshot Analysis
- **Notifications** (`133927.png`): Shows a warning banner: "Some settings are only used for new products. To change existing products: [Update existing products]". Two sections: (1) **Low stock notifications** -- Enable toggle (enabled), Low stock warning threshold (5 units). (2) **Template settings** -- Administrators who will receive notifications (dropdown), Send to channel (Email), Email Template ("Low stock notification"), SMS Template ("Low stock notification").

### Proposed Improvements for DotMac Sub

- [ ] **Create an `InventoryItem` model** with fields: id (UUID), name, sku, category_id (FK), stock_location_id (FK), quantity_on_hand, quantity_reserved, quantity_available (computed), low_stock_threshold, unit_cost, serial_number_tracking (boolean), organization_id, is_active, created_at, updated_at
- [ ] **Create an `InventoryTransaction` model** for tracking stock movements: id, item_id (FK), transaction_type (enum: receive, issue, transfer, adjust, return), quantity, from_location_id, to_location_id, reference_type (enum: service_order, subscriber, manual), reference_id, performed_by (FK to User), notes, created_at
- [ ] **Implement low stock notification alerts** via a Celery task that checks inventory levels against per-item thresholds and sends notifications to configured administrators
- [ ] **Add low stock settings to organization configuration**: enable/disable toggle, default threshold, notification channel (email/SMS/both), notification template, recipient list
- [ ] **Build inventory notification settings UI** at `/admin/inventory/settings/notifications` with threshold configuration, template selection, and recipient management
- [ ] **Integrate with existing DotMac Sub notification system** by adding EventType values: INVENTORY_LOW_STOCK, INVENTORY_OUT_OF_STOCK, INVENTORY_RECEIVED, INVENTORY_ISSUED
- [ ] **Support "Update existing products" bulk action** to apply new notification settings to all existing inventory items at once

---

## 10.12 Inventory -- Core Inventory Management (Inferred from Configuration)

### Proposed Improvements for DotMac Sub

Based on the configuration screens reviewed (stock locations, categories, notifications), the following core inventory features should be built:

- [ ] **Build inventory list view** at `/admin/inventory` showing all items with columns: SKU, Name, Category, Location, Quantity On Hand, Quantity Available, Status (in-stock/low/out-of-stock), Actions
- [ ] **Build inventory item detail view** at `/admin/inventory/<id>` showing item details, stock levels across locations, transaction history, and linked subscriber assignments
- [ ] **Implement stock receive workflow** for recording incoming equipment deliveries with quantity, supplier reference, and location
- [ ] **Implement stock issue workflow** for assigning equipment to subscribers or service orders, decrementing available quantity
- [ ] **Implement stock transfer workflow** for moving equipment between stock locations (e.g., warehouse to branch to technician vehicle)
- [ ] **Implement serial number tracking** for high-value items (routers, ONTs, switches) where each unit is individually tracked by serial number and MAC address
- [ ] **Link inventory to service orders** so that when a provisioning service order is created, required equipment is reserved from inventory and issued upon installation completion
- [ ] **Link inventory to subscriber records** so that each subscriber's detail page shows assigned equipment (CPE, ONU, router) with serial numbers
- [ ] **Build inventory dashboard** with KPI cards: Total items, Low stock items, Out of stock items, Items issued this month, and a category breakdown chart
- [ ] **Support barcode/QR code scanning** for stock receive and issue operations (future enhancement for mobile field teams)
- [ ] **Implement inventory import from CSV** for bulk loading initial stock records
- [ ] **Build inventory reports** showing stock valuation, movement history, turnover rates, and per-location breakdowns

---

## Priority Summary

### P0 -- Critical (Core CRM/Sales Pipeline)
| Feature | Effort | Impact |
|---------|--------|--------|
| Lead model and CRUD service | Medium | High -- Enables pre-sales tracking |
| Lead pipeline stages (configurable) | Medium | High -- Tracks sales funnel progression |
| Lead list/detail/create UI | Medium | High -- Admin interface for managing leads |
| Lead-to-subscriber conversion | High | High -- Closes the sales-to-provisioning loop |
| Lead event types in notification system | Low | High -- Enables automated CRM workflows |

### P1 -- High (Quoting & Inventory Foundation)
| Feature | Effort | Impact |
|---------|--------|--------|
| Quote model and CRUD service | High | High -- Formalizes pricing proposals |
| Inventory item model and stock tracking | High | High -- Tracks ISP equipment lifecycle |
| Stock location management | Low | Medium -- Organizes equipment storage |
| Inventory category management | Low | Medium -- Classifies equipment types |
| Low stock notification alerts | Medium | Medium -- Prevents equipment shortages |

### P2 -- Medium (Lead Capture & Notifications)
| Feature | Effort | Impact |
|---------|--------|--------|
| Public signup widget (lead capture form) | High | High -- Automates lead acquisition |
| Lead notification configuration | Medium | Medium -- Keeps sales team informed |
| Quote notification auto-sending | Medium | Medium -- Speeds up sales cycle |
| Lead field customization | Medium | Medium -- Adapts to ISP-specific needs |
| Inventory-to-service-order linking | High | Medium -- Streamlines provisioning |

### P3 -- Low (Advanced CRM & Inventory)
| Feature | Effort | Impact |
|---------|--------|--------|
| OTP-based quote verification | Medium | Low -- Security for quote acceptance |
| IMAP email integration | High | Low -- Niche but valuable for email-heavy ISPs |
| Email open/click tracking | High | Low -- Marketing analytics |
| Serial number tracking for inventory | Medium | Medium -- Asset management precision |
| Barcode/QR scanning support | High | Low -- Future mobile enhancement |
| Embeddable widget code generation | Medium | Low -- External website integration |

### Implementation Order Recommendation

1. **Phase 1 (Lead Foundation):** Lead model, pipeline stages, lead CRUD UI, lead event types
2. **Phase 2 (Inventory Foundation):** Inventory item model, stock locations, categories, stock transactions
3. **Phase 3 (Quoting):** Quote model, quote CRUD, quote numbering, quote PDF generation
4. **Phase 4 (Conversion & Linking):** Lead-to-subscriber conversion, inventory-to-service-order linking, inventory-to-subscriber linking
5. **Phase 5 (Automation):** Public signup widget, lead notifications, low stock alerts, quote auto-notifications
6. **Phase 6 (Advanced):** IMAP integration, email tracking, OTP verification, serial number tracking

---

*Document generated from analysis of 15 Splynx screenshots (12 leads, 3 inventory) captured 2026-02-24.*
