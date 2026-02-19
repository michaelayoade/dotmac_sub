# DotMac Sub User Guide

A comprehensive guide for using the DotMac Subscription Management platform.

---

## Table of Contents

1. [Getting Started](#getting-started)
2. [Admin Portal](#admin-portal)
3. [Customer Portal](#customer-portal)
4. [Reseller Portal](#reseller-portal)
5. [Common Tasks & Workflows](#common-tasks--workflows)
6. [Tips & Shortcuts](#tips--shortcuts)

---

## Getting Started

### System Requirements

- Modern web browser (Chrome, Firefox, Safari, Edge)
- Stable internet connection
- Screen resolution: 1280x720 minimum (1920x1080 recommended)

### Accessing the System

| Portal | URL | Who Uses It |
|--------|-----|-------------|
| Admin Portal | `https://your-domain.com/admin` | Staff & Administrators |
| Customer Portal | `https://your-domain.com/portal` | End Customers |
| Reseller Portal | `https://your-domain.com/reseller` | Partner Resellers |

### Logging In

1. Navigate to your portal URL
2. Enter your **username** or **email address**
3. Enter your **password**
4. Click **Sign In**
5. If MFA is enabled, enter the verification code from your authenticator app

### Navigation Basics

- **Sidebar** (Admin): Click menu items to navigate; click section headers to expand/collapse
- **Top Navigation** (Other Portals): Click menu items in the horizontal navbar
- **Dark Mode**: Click the moon/sun icon in the header to toggle
- **User Menu**: Click your avatar/initials in the top-right corner for profile options

---

## Admin Portal

The Admin Portal is the central hub for managing all aspects of your subscriber management system.

### Dashboard Overview

The dashboard provides a real-time snapshot of your business:

![Dashboard Layout]

| Section | Description |
|---------|-------------|
| **Network Health** | OLT/ONT status, active alarms, and connectivity metrics |
| **Revenue Trends** | Monthly recurring revenue (MRR) charts and growth |
| **Service Orders** | Pipeline view of pending, in-progress, and completed orders |
| **Key Metrics** | MRR, ARPU, Active Subscribers |
| **Billing Health** | AR aging breakdown (Current, 30, 60, 90+ days) |
| **Recent Activity** | Live feed of system events |
| **Today's Dispatch** | Field technician assignments and status |

### Sidebar Navigation

The sidebar is organized into logical sections:

#### Customers Section

**Customers**
- View all customer accounts (individuals and organizations)
- Search and filter by name, account number, status
- Create new customer accounts
- View customer details and history

**Subscribers**
- Manage individual service subscribers
- Link subscribers to accounts
- View subscription details and status
- Manage subscriber lifecycle

#### Services & Catalog

**Products**
- Define service products (Internet, Voice, TV, etc.)
- Set pricing and billing cycles
- Configure product attributes

**Speed Tiers**
- Create bandwidth tiers (e.g., 100 Mbps, 500 Mbps, 1 Gbps)
- Set download/upload speeds
- Link to products

**Offers & Promos**
- Create promotional offers
- Set discount percentages or fixed amounts
- Define validity periods
- Apply to specific products or plans

**Inventory**
- Track physical equipment (modems, routers, ONTs)
- Manage stock levels
- Assign equipment to subscribers

#### Network Section

**Network Map**
- Interactive GIS map showing network infrastructure
- View POP sites, fiber routes, and customer locations
- Click markers for details

**POP Sites**
- Manage Point of Presence locations
- View site details and equipment
- Track site status

**Core Network**

| Feature | Description |
|---------|-------------|
| All Core Devices | Complete list of core infrastructure |
| Core Routers | Border and core routing equipment |
| Distribution Switches | Distribution layer switches |
| Access Switches | Access layer equipment |
| Aggregation Devices | Traffic aggregation equipment |

**GPON Infrastructure**

| Feature | Description |
|---------|-------------|
| OLTs | Optical Line Terminals - manage head-end equipment |
| ONTs / CPE | Customer premise equipment management |
| All PON Devices | Complete PON device inventory |

**Fiber Plant / ODN**

| Feature | Description |
|---------|-------------|
| Fiber Map | Visual fiber route mapping |
| FDH Cabinets | Fiber Distribution Hub management |
| Splitters | Optical splitter tracking |
| Fiber Strands | Individual strand management |
| Splice Closures | Splice point documentation |
| Fiber Reports | Fiber plant analytics |

**IP / VLAN Management**

| Feature | Description |
|---------|-------------|
| IP Pools & Blocks | Manage IP address allocation |
| VLANs | Virtual LAN configuration |

**RADIUS / AAA**
- Configure authentication servers
- Manage RADIUS profiles
- Set bandwidth policies

**Network Monitoring**
- Real-time alarm dashboard
- Device status monitoring
- Performance metrics

#### Operations Section

**Service Orders**
- Track subscriber provisioning requests (new installs, upgrades, disconnects)
- Review order progress and status history
- Monitor open and completed installation-related tasks

#### Billing Section

**Overview**
- Billing dashboard with key metrics
- Revenue summary
- Payment trends

**Accounts**
- View billing accounts
- Account balance and history
- Payment methods on file

**Invoices**
- Generate individual or batch invoices
- View invoice details
- Send invoice notifications
- Download PDF invoices

**Payments**
- Record payments (cash, check, card, bank transfer)
- Process refunds
- Payment reconciliation

**AR Aging**
- Accounts receivable aging report
- Filter by aging bucket (Current, 30, 60, 90+ days)
- Collection priority list

**Dunning**
- Automated collection workflows
- Configure dunning schedules
- Track dunning actions

**General Ledger**
- Financial transaction journal
- Account balances
- Transaction history

**Tax Rates**
- Configure tax rates by region
- Set tax categories
- Manage tax exemptions

#### Reports Section

**Revenue Reports**
- Monthly/quarterly revenue analysis
- Revenue by product/service
- Growth trends and forecasts

**Subscriber Reports**
- Subscriber growth metrics
- Acquisition and churn rates
- Subscriber demographics

**Churn Analysis**
- Churn rate tracking
- Churn reasons analysis
- At-risk subscriber identification

**Network Reports**
- Bandwidth utilization
- Device uptime statistics
- Capacity planning data

**Technician Reports**
- Work order completion rates
- Average resolution time
- Technician performance metrics

#### Integrations Section

**Connectors**
- Configure external system connections
- API credentials management
- Connection status monitoring

**Integration Targets**
- Define integration endpoints
- Map data fields
- Configure sync schedules

**Jobs**
- View scheduled integration jobs
- Manual job execution
- Job history and logs

**Webhooks**
- Configure outbound webhooks
- Event triggers
- Delivery monitoring

**Payment Providers**
- Configure payment gateways
- Test connections
- Transaction settings

#### System Section

**Users**
- Create and manage user accounts
- Assign roles and permissions
- Reset passwords
- Enable/disable accounts

**Roles & Permissions**
- Define user roles
- Configure granular permissions
- Role-based access control

**API Keys**
- Generate API keys for integrations
- Set key permissions
- Track API usage

**Audit Log**
- View system activity history
- Filter by user, action, date
- Export audit data

**Tasks & Scheduler**
- View scheduled system tasks
- Configure task schedules
- Monitor task execution

**Legal Documents**
- Manage terms of service
- Privacy policy management
- Document versioning

**Settings**
- System configuration
- Company information
- Default values and preferences

---

## Customer Portal

The Customer Portal allows subscribers to manage their accounts, services, and billing.

### Dashboard

The customer dashboard shows:

| Widget | Description |
|--------|-------------|
| **Account Balance** | Current balance (green = credit, red = amount due) |
| **Next Bill** | Upcoming bill amount and due date |
| **Service Status** | Active or Suspended indicator |
| **Active Services** | List of subscribed services with speeds and costs |
| **Quick Actions** | Shortcuts to common tasks |
| **Recent Activity** | Timeline of recent account events |

### Quick Actions

- **Make a Payment** - Pay your bill online
- **View Invoices** - Access billing history
- **Update Profile** - Change contact information

### Services

View your active services:

- Service name and type
- Speed tier (download/upload)
- Service address
- Monthly cost
- Service status

### Billing

**View Invoices**
1. Navigate to **Billing**
2. See list of all invoices with status (Paid, Unpaid, Overdue)
3. Click an invoice to view details
4. Download PDF for your records

**Billing Arrangements**
1. Navigate to **Billing** > **Arrangements**
2. Create a new arrangement request if your account is eligible
3. Track arrangement status and upcoming due dates

### Installations

- View scheduled installation appointments
- Check installation status
- Reschedule if needed
- View installation history

### Service Orders

- Track new service requests
- View order progress
- Estimated completion dates

### Profile Settings

**Update Contact Information**
1. Click your avatar > **Profile**
2. Update name, email, phone
3. Save changes

**Change Password**
1. Click your avatar > **Security**
2. Enter current password
3. Enter new password (twice)
4. Save changes

---

## Reseller Portal

The Reseller Portal allows partners to manage their customer accounts.

### Dashboard

| Metric | Description |
|--------|-------------|
| **Total Accounts** | Number of customer accounts under management |
| **Open Balance** | Total outstanding balance across all accounts |
| **Open Invoices** | Number of unpaid invoices |

**Recent Accounts Table**
- Customer Name
- Account Number
- Status (Active, Suspended, etc.)
- Open Balance
- Last Payment Date
- Actions

### Accounts Management

**View All Accounts**
1. Navigate to **Accounts**
2. See complete list of your customer accounts
3. Search by name or account number
4. Filter by status

**Account Details**
- Click an account to view full details
- Account information
- Service subscriptions
- Billing history

### View as Customer

This feature allows you to see exactly what your customer sees:

1. Find the customer account
2. Click **View as Customer**
3. You'll be logged into the Customer Portal as that customer
4. A yellow banner shows you're in impersonation mode
5. Click **Stop Impersonation** to return to Reseller Portal

> **Note**: All actions taken while impersonating are logged for audit purposes.

---

## Common Tasks & Workflows

### Onboarding a New Customer (Admin)

```
1. Create Customer Account
   Admin > Customers > New Customer
   ↓
2. Add Subscriber
   Admin > Subscribers > New Subscriber
   Link to customer account
   ↓
3. Create Subscription
   Select subscriber > Add Subscription
   Choose product/plan
   ↓
4. Create Service Order
   Subscriber Detail > Add Subscription
   Set subscription status to Pending to trigger service order workflow
   ↓
5. Track Installation
   Use the subscriber and customer detail views to monitor service order progress
   ↓
6. Complete Installation
   Mark provisioning complete and update service status
   ↓
7. Activate Service
   Provision network (RADIUS, VLAN)
   Generate first invoice
```

### Running Monthly Billing (Admin)

```
1. Review Unbilled Subscriptions
   Admin > Billing > Overview
   Check billing queue
   ↓
2. Generate Invoices
   Admin > Billing > Invoices > Generate Batch
   Select billing period
   Preview invoices
   ↓
3. Review Generated Invoices
   Check for errors
   Adjust if needed
   ↓
4. Send Invoices
   Approve batch
   Invoices sent via email
   ↓
5. Monitor Payments
   Admin > Billing > Payments
   Record incoming payments
   ↓
6. Review AR Aging
   Admin > Billing > AR Aging
   Identify overdue accounts
   ↓
7. Run Dunning (if configured)
   Automated reminders sent
   Follow up on delinquent accounts
```

### Adding Network Equipment (Admin)

```
1. Create POP Site (if new location)
   Admin > Network > POP Sites > New
   Enter location details
   ↓
2. Add OLT
   Admin > Network > GPON > OLTs > New
   Configure OLT settings
   Assign to POP site
   ↓
3. Add ONT/CPE
   Admin > Network > GPON > ONTs > New
   Enter serial number
   Assign to subscriber
   ↓
4. Configure VLAN
   Admin > Network > IP/VLAN > VLANs
   Create or assign VLAN
   ↓
5. Create RADIUS Profile
   Admin > Network > RADIUS
   Set authentication parameters
   Configure bandwidth limits
   ↓
6. Test Connectivity
   Verify ONT registration
   Test customer connection
```

---

## Tips & Shortcuts

### Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `/` | Focus search box |
| `Esc` | Close modal/dropdown |
| `?` | Show keyboard shortcuts (if enabled) |

### Search Tips

- Use quotes for exact phrases: `"John Smith"`
- Search by account number: `ACC-12345`
- Filter by status: `status:active`

### Dashboard Customization

- Drag widgets to rearrange (if enabled)
- Click refresh icon to update data
- Use date pickers to change time ranges

### Bulk Operations

Many list views support bulk operations:

1. Check the checkbox in the header to select all
2. Or check individual items
3. Use the bulk action dropdown
4. Common bulk actions:
   - Export to CSV
   - Send notifications
   - Update status
   - Delete

### Export Data

Most tables support data export:

1. Navigate to the list view
2. Apply desired filters
3. Click **Export** button
4. Choose format (CSV, Excel, PDF)
5. Download file

### Dark Mode

Toggle dark mode for comfortable viewing:

1. Click the sun/moon icon in the header
2. Or: User menu > Settings > Appearance
3. Preference is saved automatically

### Mobile Access

The portals are mobile-responsive:

- Sidebar collapses to hamburger menu
- Tables become scrollable cards
- Forms adapt to screen size
- Touch-friendly buttons and controls

---

## Getting Help

### In-App Help

- Look for `?` icons next to features for tooltips
- Check the Help section in your user menu

### Support Contacts

- **Technical Support**: support@your-domain.com
- **Billing Questions**: billing@your-domain.com
- **Phone**: +1 (XXX) XXX-XXXX

### Training Resources

- Video tutorials (if available)
- Knowledge base articles
- Release notes for new features

---

## Glossary

| Term | Definition |
|------|------------|
| **Account** | A billing entity (person or organization) |
| **Subscriber** | An individual service user linked to an account |
| **Subscription** | A service plan assigned to a subscriber |
| **OLT** | Optical Line Terminal - head-end fiber equipment |
| **ONT** | Optical Network Terminal - customer fiber modem |
| **CPE** | Customer Premise Equipment |
| **GPON** | Gigabit Passive Optical Network |
| **VLAN** | Virtual Local Area Network |
| **RADIUS** | Remote Authentication Dial-In User Service |
| **MRR** | Monthly Recurring Revenue |
| **ARPU** | Average Revenue Per User |
| **AR** | Accounts Receivable |
| **Dunning** | Collection process for overdue payments |
| **POP** | Point of Presence - network location |
| **FDH** | Fiber Distribution Hub |
| **Service Order** | Request for new service or changes |

---

*Last Updated: February 16, 2026*
*Version: 1.0*
