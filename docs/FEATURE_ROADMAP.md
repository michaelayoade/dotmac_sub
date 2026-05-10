# DotMac Feature Improvement Roadmap

*Comparison with Splynx v4.0-v5.2 feature set*

---

## Category 1: BILLING AUTOMATION
*Reduce manual intervention, improve cash flow*

### 1.1 Auto Invoice Charging
**Priority: P0 (Critical)** | **Effort: Medium** | **Splynx: v5.1**

Automatically charge issued invoices against stored payment methods.

**Specifications:**
- Charging modes: End-of-day batch, Hourly, Immediate on issue
- Configurable per billing channel (Stripe auto-charge, PayPal skip)
- Retry logic: 3 attempts over 7 days with exponential backoff
- Failure handling: Mark invoice as charge-failed, trigger dunning
- Settings: `billing.auto_charge.enabled`, `billing.auto_charge.mode`, `billing.auto_charge.retry_count`
- Exclude: Invoices with payment arrangements, disputed invoices
- Audit: Log all charge attempts with gateway response

**Data Model:**
```python
class InvoiceChargeAttempt(Base):
    invoice_id: UUID
    payment_method_id: UUID
    attempted_at: datetime
    status: Enum[pending, succeeded, failed, skipped]
    gateway_response: JSON
    failure_reason: str | None
    retry_number: int
```

**UI:**
- Invoice list: "Auto-charge" status column
- Invoice detail: Charge attempt history
- Settings: Auto-charge configuration per organization
- Dashboard: Failed charges widget

---

### 1.2 Linked Accounts (Consolidated Billing)
**Priority: P1 (High)** | **Effort: Large** | **Splynx: v5.1**

Allow multiple subscriber accounts to roll up to a single billing entity.

**Specifications:**
- Parent/child account hierarchy (max 2 levels)
- Child accounts: Own services, usage, support tickets
- Parent account: Receives consolidated invoice for all children
- Billing modes:
  - Consolidated (single invoice for all)
  - Summary (parent invoice + child detail invoices)
  - Pass-through (children billed directly, parent sees reports)
- Payment: Parent payment method used for all children
- Reporting: MRR attributed to parent, breakout by child available

**Data Model:**
```python
class SubscriberAccountLink(Base):
    parent_subscriber_id: UUID
    child_subscriber_id: UUID
    link_type: Enum[billing, reporting, full]
    billing_mode: Enum[consolidated, summary, passthrough]
    effective_from: date
    effective_to: date | None
    created_by: UUID
```

**UI:**
- Subscriber detail: "Linked Accounts" tab
- Add/remove child accounts
- Consolidated invoice preview
- Parent dashboard: All children summary

---

### 1.3 Tax Update Tool
**Priority: P2 (Medium)** | **Effort: Medium** | **Splynx: v5.2**

Bulk update tax rates with safety mechanisms.

**Specifications:**
- Bulk operations: Update rate, change tax group, reassign locations
- Preview mode: Show affected subscribers, subscriptions, future invoices
- Backup: Snapshot current tax assignments before change
- Rollback: Restore from backup within 30 days
- Effective date: Schedule tax changes for future date
- Audit: Full change log with before/after values

**UI:**
- Tax management: "Bulk Update" wizard
- Step 1: Select tax rates to modify
- Step 2: Preview impact (subscriber count, invoice estimates)
- Step 3: Confirm and execute (or schedule)
- History: List of bulk updates with rollback option

---

### 1.4 Prorated Cancellation Refunds
**Priority: P1 (High)** | **Effort: Medium** | **Splynx: v5.1**

Automatic refund calculation when services are cancelled mid-cycle.

**Specifications:**
- Calculate unused portion of prepaid period
- Refund methods: Credit note, payment refund, account credit
- Configurable per offer: No refund, prorated, full period
- Early termination fees: Deduct from refund if applicable
- Minimum refund threshold: Skip refunds below $X

**Data Model:**
```python
class CancellationRefundPolicy(Base):
    offer_id: UUID
    refund_type: Enum[none, prorated, full_period]
    early_termination_fee: Decimal | None
    minimum_refund_amount: Decimal
    refund_method: Enum[credit_note, payment_refund, account_credit]
```

---

## Category 2: CUSTOMER SELF-SERVICE
*Reduce support burden, improve customer experience*

### 2.1 Customer Portal 2FA
**Priority: P0 (Critical)** | **Effort: Small** | **Splynx: v4.2**

Two-factor authentication for customer portal login.

**Specifications:**
- Methods: TOTP (Google Authenticator), SMS, Email OTP
- Enforcement: Optional, Required for all, Required for business accounts
- Recovery: Backup codes (10 single-use codes)
- Remember device: 30-day trusted device option
- Admin override: Support can temporarily disable for account recovery

**Data Model:**
```python
class SubscriberMfaMethod(Base):
    subscriber_id: UUID
    method_type: Enum[totp, sms, email]
    secret: str  # encrypted
    is_primary: bool
    verified_at: datetime | None

class SubscriberMfaBackupCode(Base):
    subscriber_id: UUID
    code_hash: str
    used_at: datetime | None
```

**UI:**
- Portal settings: "Security" section
- Enable/disable 2FA
- Manage methods and backup codes
- Login flow: 2FA challenge screen

---

### 2.2 Self-Service Cancellation
**Priority: P1 (High)** | **Effort: Medium** | **Splynx: v5.2**

Allow customers to cancel services from portal.

**Specifications:**
- Cancellation flow: Reason selection → Retention offer → Confirmation
- Retention offers: Discount, pause, downgrade suggestions
- Cooling-off period: 24-48hr window to reverse
- Effective date: Immediate, end of billing period, custom date
- Restrictions: Cannot cancel with outstanding balance > $X
- Notifications: Email confirmation, admin alert

**Data Model:**
```python
class CancellationRequest(Base):
    subscription_id: UUID
    requested_by: UUID  # subscriber user
    reason_code: str
    reason_text: str | None
    effective_date: date
    retention_offer_id: UUID | None
    retention_accepted: bool
    status: Enum[pending, confirmed, reversed, completed]
    confirmed_at: datetime | None
```

**UI:**
- Portal: "Cancel Service" button on subscription
- Multi-step wizard with retention offers
- Confirmation page with refund estimate
- Cancellation history

---

### 2.3 Blocked Customer DNS Redirect
**Priority: P1 (High)** | **Effort: Medium** | **Splynx: v5.1**

Redirect blocked customers to a landing page instead of hard block.

**Specifications:**
- Landing page: Customizable HTML template per organization
- Information displayed: Outstanding balance, payment link, support contact
- Payment integration: Direct payment button on landing page
- Auto-unblock: Unblock within 5 minutes of successful payment
- Bypass: Allow specific domains (payment gateways, support portal)
- Implementation: DNS redirect or captive portal via RADIUS CoA

**Data Model:**
```python
class BlockedCustomerRedirect(Base):
    organization_id: UUID
    landing_page_template: str
    bypass_domains: list[str]
    payment_link_enabled: bool
    support_contact_enabled: bool
    auto_unblock_enabled: bool
```

**UI:**
- Admin: Blocked redirect template editor
- Preview mode
- Bypass domain configuration
- Customer view: Landing page with payment option

---

### 2.4 Referral Program
**Priority: P3 (Low)** | **Effort: Medium** | **Splynx: v4.2**

Customer referral tracking and rewards.

**Specifications:**
- Referral codes: Unique per customer, shareable link
- Rewards: Account credit, discount, free month
- Tiers: Different rewards for 1st, 5th, 10th referral
- Tracking: Click tracking, conversion tracking
- Fraud prevention: Same household detection, minimum tenure requirement
- Reporting: Referral leaderboard, conversion rates

**Data Model:**
```python
class ReferralProgram(Base):
    organization_id: UUID
    name: str
    referrer_reward_type: Enum[credit, discount, free_period]
    referrer_reward_value: Decimal
    referee_reward_type: Enum[credit, discount, free_period]
    referee_reward_value: Decimal
    min_referrer_tenure_days: int

class Referral(Base):
    program_id: UUID
    referrer_subscriber_id: UUID
    referee_subscriber_id: UUID | None
    referral_code: str
    clicked_at: datetime | None
    converted_at: datetime | None
    reward_issued_at: datetime | None
```

---

## Category 3: NETWORK OPERATIONS
*Improve NOC efficiency and network reliability*

### 3.1 RADIUS Failover
**Priority: P1 (High)** | **Effort: Medium** | **Splynx: v4.1**

Backup RADIUS server for high availability.

**Specifications:**
- Primary/secondary RADIUS server designation
- Automatic failover: NAS devices switch on primary timeout
- Health monitoring: Periodic auth test to primary
- Failback: Automatic return to primary when healthy
- Sync: Real-time user/session sync between servers
- Alerting: Notify NOC on failover events

**Data Model:**
```python
class RadiusServerCluster(Base):
    name: str
    primary_server_id: UUID
    secondary_server_id: UUID
    failover_timeout_ms: int
    health_check_interval_sec: int
    auto_failback: bool

class RadiusServerHealth(Base):
    server_id: UUID
    checked_at: datetime
    is_healthy: bool
    latency_ms: int | None
    error_message: str | None
```

---

### 3.2 Multi-PSK for MDUs
**Priority: P2 (Medium)** | **Effort: Large** | **Splynx: v5.1/5.2**

Multiple pre-shared keys per WiFi network for apartment buildings.

**Specifications:**
- Per-unit PSK: Each apartment gets unique WiFi password
- VLAN mapping: PSK → VLAN → subscriber isolation
- Bulk provisioning wizard: Import unit list, auto-generate PSKs
- Self-service: Tenant can view/regenerate PSK in portal
- Rotation: Scheduled PSK rotation with advance notice
- Device limit: Max devices per PSK

**Data Model:**
```python
class WifiNetwork(Base):
    name: str
    ssid: str
    site_id: UUID  # MDU building
    vlan_pool_start: int
    vlan_pool_end: int

class WifiPsk(Base):
    network_id: UUID
    subscriber_id: UUID
    unit_identifier: str  # Apt 101
    psk: str  # encrypted
    vlan_id: int
    device_limit: int
    expires_at: datetime | None
    last_rotated_at: datetime | None
```

**UI:**
- Network: MDU WiFi management
- Bulk import wizard (CSV: unit, subscriber)
- Per-unit PSK view/regenerate
- Customer portal: "My WiFi" section

---

### 3.3 NetFlow Traffic Analysis
**Priority: P3 (Low)** | **Effort: Large** | **Splynx: v4.0**

Network traffic analysis and statistics.

**Specifications:**
- Collectors: NetFlow v5/v9, IPFIX, sFlow
- Storage: Time-series database (InfluxDB/TimescaleDB)
- Per-subscriber: Top applications, bandwidth by protocol
- Network-wide: Top talkers, traffic patterns, anomaly detection
- Retention: Configurable (7 days detail, 90 days aggregates)
- Integration: Link flows to subscriber via IP assignment

**Data Model:**
```python
class NetflowCollector(Base):
    name: str
    listen_port: int
    protocol: Enum[netflow_v5, netflow_v9, ipfix, sflow]

class TrafficSummary(Base):  # Aggregated hourly
    subscriber_id: UUID
    hour: datetime
    bytes_in: int
    bytes_out: int
    top_protocols: JSON  # {protocol: bytes}
    top_destinations: JSON  # {ip: bytes}
```

---

## Category 4: TASK & SCHEDULING
*Field technician and NOC workflow improvements*

### 4.1 Task Labels (Color-Coded)
**Priority: P2 (Medium)** | **Effort: Small** | **Splynx: v5.1**

Visual categorization of tasks.

**Specifications:**
- Labels: User-defined with color and icon
- Pre-built: Urgent (red), Installation (blue), Repair (orange), Follow-up (yellow)
- Multi-label: Tasks can have multiple labels
- Filtering: Filter task list by label
- Bulk apply: Apply labels to multiple tasks

**Data Model:**
```python
class TaskLabel(Base):
    organization_id: UUID
    name: str
    color: str  # hex
    icon: str | None
    sort_order: int

class TaskLabelAssignment(Base):
    task_id: UUID
    label_id: UUID
```

---

### 4.2 Mass Task Actions
**Priority: P2 (Medium)** | **Effort: Small** | **Splynx: v5.0**

Bulk operations on multiple tasks.

**Specifications:**
- Actions: Close, Archive, Reassign, Add label, Change priority
- Selection: Checkbox multi-select, Select all filtered
- Confirmation: Preview affected tasks before action
- Audit: Log bulk action with all affected task IDs

**UI:**
- Task list: Checkbox column
- Bulk action toolbar appears on selection
- Confirmation modal with task count

---

### 4.3 Scheduling Calendar
**Priority: P2 (Medium)** | **Effort: Large** | **Splynx: v4.3**

Visual calendar for field technician scheduling.

**Specifications:**
- Views: Day, Week, Month, Technician
- Drag-and-drop: Reschedule appointments
- Travel time: Auto-calculate between appointments (Google Maps API)
- Conflicts: Warn on overlapping appointments
- Technician availability: Working hours, PTO, lunch breaks
- Color coding: By task type, status, or technician

**Data Model:**
```python
class TechnicianSchedule(Base):
    user_id: UUID
    day_of_week: int
    start_time: time
    end_time: time

class TechnicianTimeOff(Base):
    user_id: UUID
    start_date: date
    end_date: date
    reason: str

class AppointmentSlot(Base):
    task_id: UUID
    technician_id: UUID
    scheduled_start: datetime
    scheduled_end: datetime
    travel_time_minutes: int
    location_lat: Decimal
    location_lng: Decimal
```

**UI:**
- Scheduling: Full-page calendar view
- Technician sidebar with availability
- Drag appointments to reschedule
- Click to create new appointment

---

### 4.4 Inventory + Scheduling Integration
**Priority: P3 (Low)** | **Effort: Medium** | **Splynx: v5.1**

Link inventory items to scheduled tasks.

**Specifications:**
- Task equipment list: Items needed for task
- Auto-reserve: Reserve inventory when task scheduled
- Release: Return to stock if task cancelled
- Consumption: Mark items as used/installed on completion
- Shortage alerts: Warn if scheduled tasks exceed stock

**Data Model:**
```python
class TaskEquipmentRequirement(Base):
    task_id: UUID
    inventory_item_id: UUID
    quantity_required: int
    quantity_reserved: int
    quantity_used: int | None
    reservation_expires_at: datetime
```

---

## Category 5: COMMUNICATION
*Improve customer engagement*

### 5.1 WhatsApp Enhancements
**Priority: P2 (Medium)** | **Effort: Medium** | **Splynx: v5.2**

Improve WhatsApp integration.

**Sub-features:**
- **WYSIWYG toolbar**: Bold, italic, strikethrough, code, lists
- **Unanswered alerts**: Notify agent if chat unanswered for 5/15/30 min
- **Canned responses**: Quick reply templates with placeholders
- **Phone relinking**: Move phone number between customers with history

**Data Model:**
```python
class WhatsAppCannedResponse(Base):
    organization_id: UUID
    name: str
    content: str  # with {{placeholders}}
    category: str | None

class WhatsAppUnansweredAlert(Base):
    conversation_id: UUID
    alert_threshold_minutes: int
    alerted_at: datetime | None
    acknowledged_by: UUID | None
```

---

### 5.2 Ticket Feedback Surveys
**Priority: P3 (Low)** | **Effort: Small** | **Splynx: v4.1**

Customer satisfaction measurement.

**Specifications:**
- Survey trigger: On ticket close
- Rating: 1-5 stars or thumbs up/down
- Optional comment: Free text feedback
- Reporting: CSAT score by agent, category, time period
- Follow-up: Auto-create task for low ratings

**Data Model:**
```python
class TicketFeedback(Base):
    ticket_id: UUID
    rating: int  # 1-5
    comment: str | None
    submitted_at: datetime
    follow_up_task_id: UUID | None
```

---

## Category 6: INVENTORY
*Stock management improvements*

### 6.1 Low-Stock Alerts
**Priority: P2 (Medium)** | **Effort: Small** | **Splynx: v4.1**

Per-product stock threshold notifications.

**Specifications:**
- Thresholds: Warning level, Critical level per product
- Notifications: Email, in-app, webhook
- Recipients: Configurable per product or category
- Snooze: Temporarily disable alerts for restocking period

**Data Model:**
```python
class InventoryStockThreshold(Base):
    product_id: UUID
    location_id: UUID | None  # null = all locations
    warning_level: int
    critical_level: int
    notification_recipients: list[UUID]
    snoozed_until: datetime | None
```

---

### 6.2 Stock Location Permissions
**Priority: P3 (Low)** | **Effort: Small** | **Splynx: v5.1**

Per-location inventory access control.

**Specifications:**
- Permissions: View, Transfer In, Transfer Out, Adjust
- Role-based: Assign location permissions to roles
- Transfer approval: Require approval for inter-location transfers

**Data Model:**
```python
class InventoryLocationPermission(Base):
    role_id: UUID
    location_id: UUID
    can_view: bool
    can_transfer_in: bool
    can_transfer_out: bool
    can_adjust: bool
```

---

## Category 7: ADMIN UX
*Improve administrator experience*

### 7.1 Global Search Shortcut
**Priority: P2 (Medium)** | **Effort: Small** | **Splynx: v4.0**

Keyboard shortcut for system-wide search.

**Specifications:**
- Shortcut: `/` or `s` key opens search modal
- Search scope: Subscribers, invoices, ONTs, tickets, orders
- Results: Grouped by entity type with quick actions
- Recent: Show recent searches
- Keyboard navigation: Arrow keys to navigate, Enter to select

---

### 7.2 Welcome Tour & Deployment Guide
**Priority: P3 (Low)** | **Effort: Medium** | **Splynx: v4.0**

Onboarding for new administrators.

**Specifications:**
- Welcome tour: Step-by-step UI walkthrough (10-15 steps)
- Deployment checklist: Configuration tasks with progress
- Knowledge base links: Contextual documentation
- Skip option: Experienced users can dismiss
- Resume: Continue where left off

**Data Model:**
```python
class AdminOnboardingProgress(Base):
    user_id: UUID
    tour_completed: bool
    tour_step: int
    checklist_items: JSON  # {item: completed}
    dismissed_at: datetime | None
```

---

### 7.3 Force Admin 2FA
**Priority: P1 (High)** | **Effort: Small** | **Splynx: v5.0**

Require MFA for all admin accounts.

**Specifications:**
- Organization setting: `security.admin_mfa_required`
- Grace period: 7 days to set up after enabling
- Enforcement: Block login until MFA configured
- Bypass: Super-admin can temporarily disable for support

---

## Category 8: INTEGRATIONS
*Expand ecosystem connectivity*

### 8.1 3CX Call Center Integration
**Priority: P3 (Low)** | **Effort: Large** | **Splynx: v4.0**

Call center integration.

**Specifications:**
- Caller ID lookup: Show subscriber info on incoming call
- Click-to-call: Dial from subscriber profile
- Call logging: Store call records linked to subscriber
- Ticket creation: Create ticket from call with recording link

---

### 8.2 Accounting Integrations (NetSuite/Zoho)
**Priority: P3 (Low)** | **Effort: Large** | **Splynx: v4.3**

Sync financial data to accounting systems.

**Specifications:**
- Sync entities: Invoices, payments, credit notes, customers
- Direction: DotMac → Accounting (one-way)
- Mapping: Chart of accounts, tax codes, payment methods
- Frequency: Real-time or scheduled batch

---

## Implementation Phases

### Phase 1: Foundation (Q3 2026)
| Feature | Priority | Effort |
|---------|----------|--------|
| Customer Portal 2FA | P0 | Small |
| Force Admin 2FA | P1 | Small |
| Auto Invoice Charging | P0 | Medium |
| Task Labels | P2 | Small |
| Mass Task Actions | P2 | Small |
| Low-Stock Alerts | P2 | Small |
| Global Search Shortcut | P2 | Small |

### Phase 2: Self-Service (Q4 2026)
| Feature | Priority | Effort |
|---------|----------|--------|
| Self-Service Cancellation | P1 | Medium |
| Blocked Customer Redirect | P1 | Medium |
| Prorated Cancellation Refunds | P1 | Medium |
| RADIUS Failover | P1 | Medium |
| Tax Update Tool | P2 | Medium |

### Phase 3: Operations (Q1 2027)
| Feature | Priority | Effort |
|---------|----------|--------|
| Linked Accounts | P1 | Large |
| Scheduling Calendar | P2 | Large |
| WhatsApp Enhancements | P2 | Medium |
| Inventory + Scheduling | P3 | Medium |
| Stock Location Permissions | P3 | Small |

### Phase 4: Advanced (Q2 2027)
| Feature | Priority | Effort |
|---------|----------|--------|
| Multi-PSK for MDUs | P2 | Large |
| NetFlow Traffic Analysis | P3 | Large |
| 3CX Integration | P3 | Large |
| Accounting Integrations | P3 | Large |
| Referral Program | P3 | Medium |
| Welcome Tour | P3 | Medium |
| Ticket Feedback Surveys | P3 | Small |

---

## Summary

| Category | Features | P0 | P1 | P2 | P3 |
|----------|----------|----|----|----|----|
| Billing Automation | 4 | 1 | 2 | 1 | 0 |
| Customer Self-Service | 4 | 1 | 2 | 0 | 1 |
| Network Operations | 3 | 0 | 1 | 1 | 1 |
| Task & Scheduling | 4 | 0 | 0 | 3 | 1 |
| Communication | 2 | 0 | 0 | 1 | 1 |
| Inventory | 2 | 0 | 0 | 1 | 1 |
| Admin UX | 3 | 0 | 1 | 1 | 1 |
| Integrations | 2 | 0 | 0 | 0 | 2 |
| **Total** | **24** | **2** | **6** | **8** | **8** |

---

## DotMac Existing Advantages Over Splynx

| Feature | Notes |
|---------|-------|
| **Fiber plant management** | Full topology: FDH, splice closures, splitters, strands |
| **OLT dependency preflight** | Validates profiles before OLT writes |
| **Bridge WAN mode** | Native VLAN for bridged services |
| **Buildout projects** | Infrastructure expansion tracking |
| **Service qualification** | Address-based availability checking |
| **Churn analytics** | Prediction and tracking |
| **Zabbix integration** | Enterprise monitoring |
| **Splynx migration** | Built-in data migration |
| **Ledger system** | Double-entry accounting |
| **GIS/fiber change requests** | Geographic change management |

---

*Generated: 2026-05-08*
*Based on Splynx changelog v4.0-v5.2*
