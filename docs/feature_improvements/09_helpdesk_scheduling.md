# Section 9: Helpdesk & Scheduling

## Source: Splynx ISP Management Platform

This document captures feature improvements for the DotMac Sub ISP management system based on a comprehensive review of 28 Splynx screenshots covering helpdesk ticketing configuration and field scheduling/task management features.

---

## 9.1 Helpdesk Ticket Configuration

**Splynx Features Observed:**
The Splynx helpdesk configuration (Config > Helpdesk > Tickets) provides a comprehensive ticketing system setup with email notification controls, sender name/email configuration, CC email support, admin panel notification toggles, configurable limits on additional fields, scheduled auto-assigning of tickets to admins based on project scheduling, and default admin assignment for tickets outside scheduled hours.

### Email & Notification Settings

- [ ] **Add helpdesk email configuration settings** -- Provide configurable fields for sender name, sender email address, and CC/copy email for all ticket notifications. Allow operators to brand outgoing support emails with their ISP name (e.g., "DOTMAC SUPPORT") and route copies to a shared helpdesk inbox.
- [ ] **Add toggle for ticket email notifications** -- Allow admins to enable/disable email sending when tickets are updated, and separately control whether file attachments are included in notification emails.
- [ ] **Add admin panel real-time notifications for tickets** -- Send in-app notifications to admin users when they are online and a ticket is created, updated, or assigned to them. Display a notification badge or toast in the admin sidebar.
- [ ] **Add configurable additional fields for tickets** -- Allow operators to define custom fields on tickets (e.g., site location, equipment serial number, signal level) with a configurable limit on the number of visible additional fields.

### Ticket Auto-Assignment

- [ ] **Implement scheduled ticket auto-assignment** -- Automatically assign incoming tickets to available support agents based on a scheduling project. During scheduled hours, tickets are routed to on-duty agents; outside those hours, they fall back to configurable default admins.
- [ ] **Add round-robin ticket assignment** -- Support assignment strategies including random admin selection, round-robin rotation, and least-loaded agent. Allow configuration of which admins are eligible for auto-assignment.
- [ ] **Add notification routing for ticket assignment** -- Configure how assignment notifications are sent (to random admin, specific admin, or group of admins) to ensure timely ticket pickup.

---

## 9.2 Ticket Status Workflow

**Splynx Features Observed:**
The "Tickets link status with action" configuration maps ticket lifecycle events (creating, opening, closing, customer reply, agent reply) to status transitions. Ticket statuses include New, Work in Progress, Resolved, Waiting on Customer, and Waiting on Agent, each with separate labels for agent and customer views, color-coded badges, open/closed/unresolved marks, dashboard icons, and dashboard visibility toggles.

### Status Configuration

- [ ] **Add configurable ticket status definitions** -- Allow operators to define custom ticket statuses with separate display titles for agents and customers (e.g., agent sees "Waiting on customer" while customer sees "Awaiting your answer"), color-coded labels (Info, Success, Warning, Default), and open/closed/unresolved marking.
- [ ] **Add ticket status-action linking** -- Automatically transition ticket status based on lifecycle events: set status to "New" on creation, "Work in Progress" on opening, "Resolved" on closing, and configurable statuses on customer reply and agent reply.
- [ ] **Add dashboard icons for ticket statuses** -- Display distinctive icons for each ticket status on the admin dashboard (e.g., open arrow for New, headset for Work in Progress, checkmark for Resolved, clock for Waiting on Customer, people icon for Waiting on Agent).
- [ ] **Add ticket status dashboard visibility control** -- Allow operators to configure which ticket statuses appear on the admin dashboard and which are hidden (e.g., hide "Waiting on customer" from dashboard counts to reduce noise).

---

## 9.3 Ticket Types (ISP-Specific Categories)

**Splynx Features Observed:**
Splynx supports 44+ configurable ticket types tailored to ISP operations, including network-specific categories (Fiber Link Disconnection, Base Station Down, Cabinet Disconnection, Intermittent Connectivity, IP Authentication, Low Signal, Radio Issues, AP/Air Fiber Outage, BTS Outage, Cable Burn, Cable Vandalization, Core Breakage, Bad Pigtail/Patch Cord, Outdoor Box Faults, PPOE Authentication, Router Troubleshooting, Power Optimization), billing categories (Billing Issues, Expired Subscription, Payment Confirmation, Plan Downgrade, Plan Upgrade), and service categories (Service Change, Order Service Request, Migration, Drop Cable Installation, LAN Troubleshooting). Types are color-coded by category.

### Ticket Type Management

- [ ] **Add configurable ticket type taxonomy** -- Create a management interface for ISP-specific ticket types organized by category. Pre-seed with common ISP categories: Network Faults (fiber, radio, power), Billing (payment, subscription), Service Requests (installation, migration, plan changes), and Equipment (CPE, router, ONT issues).
- [ ] **Add default ticket type setting** -- Allow operators to set a default ticket type (e.g., "Inquiry") that is pre-selected when creating new tickets, reducing clicks for the most common ticket type.
- [ ] **Add ticket type color coding** -- Assign colors to ticket type categories so agents can visually distinguish network outage tickets from billing inquiries at a glance in the ticket list view.
- [ ] **Add ticket type to service order linkage** -- When a ticket type maps to a service action (e.g., "Plan Upgrade", "Migration", "Drop Cable Installation"), offer a one-click option to create a linked service order from the ticket.

---

## 9.4 Ticket Groups & Agent Assignment

**Splynx Features Observed:**
Splynx organizes agents into ticket groups (Finance, NOC, Helpdesk, Project, Sales, Tech Support, Lagos Team, regional PM teams, Store Unit, CRM, FMU, etc.) with 55+ groups for a large ISP operation. Each group has a title, description, and list of assigned agents. This enables routing tickets to the right team and regional assignment.

### Team-Based Ticket Routing

- [ ] **Add ticket group management** -- Allow operators to create ticket groups representing organizational teams (NOC, Helpdesk, Tech Support, Finance, Sales) and regional teams (e.g., Lagos Team, Abuja PMs). Assign agents to one or more groups.
- [ ] **Add default ticket group routing** -- Configure default groups for tickets coming from different sources: API-created tickets route to one group (e.g., Helpdesk), customer portal tickets to another (e.g., Any/auto-detect), and email-created tickets to a configurable default.
- [ ] **Add regional ticket routing** -- Route tickets to region-specific teams based on the subscriber's location or POP site, ensuring local technicians handle local issues.
- [ ] **Add group-based ticket visibility** -- Allow agents to see only tickets assigned to their groups, with supervisors having cross-group visibility. Support group-level SLA tracking and performance metrics.

---

## 9.5 Ticket Notifications

**Splynx Features Observed:**
Splynx provides granular ticket notification configuration with separate email and SMS toggles for: ticket received (to customer), new ticket (to admin), ticket changes (to customer/admin), new messages (to customer/admin), new note (to admin), ticket assigned (to admin), ticket closed (to customer/admin), ticket opened (to customer/admin), ticket reopened (to customer/admin), ticket status notification (to watchers), and file attachment notifications. Each notification has a configurable subject line template using placeholders like `#(ticket_id)`, `(ticket_subject)`.

### Notification Configuration

- [ ] **Add per-event ticket notification toggles** -- Provide individual enable/disable switches for each ticket notification event: creation, assignment, status change, new message, new note, close, reopen, and watcher updates. Support both email and SMS channels.
- [ ] **Add ticket notification templates with placeholders** -- Allow customization of notification subject lines and body text using template variables such as `{ticket_id}`, `{ticket_subject}`, `{customer_name}`, `{agent_name}`, `{status}`. Pre-populate with sensible defaults.
- [ ] **Add ticket watcher notifications** -- Allow agents to "watch" tickets they are interested in and receive notifications on status changes and new activity, even if they are not the assigned agent.
- [ ] **Add separate customer vs. admin notification paths** -- Route different notification content to customers (simplified, branded) versus admins (detailed, with internal context), ensuring customers never see internal notes or technical details.

---

## 9.6 Canned Responses

**Splynx Features Observed:**
Splynx provides canned response management with two levels: Canned Groups (e.g., "Help Desk/Support", "NOC") and individual Canned Responses. Each response has a title, text body, group membership, associated agent (Personal visibility), and visibility scope (Tickets). This allows agents to insert pre-written responses for common issues quickly.

### Quick Response Management

- [ ] **Add canned response library** -- Create a management interface for pre-written response templates that agents can insert into ticket replies with one click. Support rich text formatting and template variables.
- [ ] **Add canned response groups** -- Organize canned responses into groups (e.g., "Help Desk/Support", "NOC", "Billing") so agents can quickly find relevant templates for their department.
- [ ] **Add personal vs. shared canned responses** -- Allow agents to create personal canned responses visible only to them, alongside shared organizational responses visible to all agents or specific groups.
- [ ] **Add canned response search and quick-insert** -- In the ticket reply editor, provide a searchable dropdown or keyboard shortcut (e.g., typing `/` followed by keywords) to find and insert canned responses without leaving the reply interface.

---

## 9.7 Ticket Widget (Customer-Facing)

**Splynx Features Observed:**
Splynx offers an embeddable ticket widget that can be placed on external websites. Configuration includes: button type (Text/Icon), button text, background/text colors, alignment (Left/Right), offset in pixels, form title, send button text, thank-you message, HTTPS toggle, priority toggle, and ticket type toggle. The widget generates an embed code (JavaScript snippet) for placement on any website.

### Public Support Widget

- [ ] **Add embeddable support ticket widget** -- Provide a configurable pop-up or embedded widget that ISP operators can place on their public website, allowing customers to submit support tickets without logging into the customer portal.
- [ ] **Add widget appearance customization** -- Allow operators to customize the widget button text, colors (background and text), alignment (left/right), pixel offset, and form title to match their website branding.
- [ ] **Add widget form configuration** -- Configure which fields appear on the widget form: subject, description, priority selector (enable/disable), ticket type selector (enable/disable), file attachment capability, and custom thank-you message after submission.
- [ ] **Add widget embed code generator** -- Generate a copy-paste JavaScript snippet that operators can add to any external webpage, with automatic HTTPS support and lazy loading.

---

## 9.8 Email-to-Ticket (Inboxes)

**Splynx Features Observed:**
Splynx provides an email-to-ticket pipeline (Config > Helpdesk > Inboxes) with: regex pattern for identifying ticket numbers in email subjects, HTML purification for save format, subject-based pairing option, forwarded email processing from admins, incoming email processing toggle, configured incoming mailboxes (with email, group assignment, type, priority), and a deny list for filtering spam by location, filter type, and filter pattern.

### Email Integration

- [ ] **Add email-to-ticket conversion** -- Monitor configured incoming mailboxes and automatically create tickets from received emails, or append to existing tickets when the email subject contains a recognizable ticket number pattern.
- [ ] **Add ticket number regex matching** -- Use a configurable regex pattern (e.g., `#(?P<number>\d{1,})`) to identify ticket references in email subject lines and thread replies into existing tickets.
- [ ] **Add incoming mailbox configuration** -- Allow operators to configure one or more incoming mailboxes with email address, default ticket group, default ticket type, and default priority. Support enable/disable per mailbox.
- [ ] **Add email deny list / spam filtering** -- Provide a deny list to block ticket creation from specific email addresses, domains, or patterns. Track filtered counts per day and total for monitoring spam volume.
- [ ] **Add HTML email sanitization** -- Sanitize incoming HTML emails using a purification library before storing ticket content, preventing XSS and rendering issues while preserving formatting.
- [ ] **Add admin email forwarding support** -- Allow admins to forward customer emails to the helpdesk inbox and have them automatically converted to tickets, with the original sender identified as the ticket requester.

---

## 9.9 Ticket Automation

**Splynx Features Observed:**
Splynx provides a ticket automation rules engine (Config > Helpdesk > Ticket Automation) with columns for: Rule Priority, Current Status, Current Priority, Time Passed, Change Status To, and Change Priority To. This allows time-based escalation rules (e.g., if a ticket has been in "New" status with "High" priority for more than 2 hours, auto-escalate to "Critical" and change status to "Work in Progress").

### Automation Rules

- [ ] **Add ticket automation rules engine** -- Create a rules-based automation system that monitors ticket attributes and applies changes after configurable time thresholds. Rules specify current status, current priority, time elapsed, target status, and target priority.
- [ ] **Add time-based ticket escalation** -- Automatically escalate ticket priority when a ticket remains unresolved beyond a defined time threshold (e.g., escalate from Medium to High after 4 hours, from High to Critical after 8 hours).
- [ ] **Add SLA-based status transitions** -- Automatically change ticket status based on SLA timers (e.g., move "Waiting on agent" tickets to "Overdue" if no agent response within configured SLA window).
- [ ] **Add automation rule priority ordering** -- Allow operators to set execution priority for automation rules so that more specific rules take precedence over general ones, preventing conflicts.

---

## 9.10 Scheduling Workflows

**Splynx Features Observed:**
Splynx provides a scheduling workflow configuration (Config > Scheduling > Workflows) with named workflow definitions: Default, Installation Workflow, Maintenance Workflow, Migration Workflow, and Test. Each workflow has view, edit, and delete actions.

### Workflow Management

- [ ] **Add scheduling workflow definitions** -- Create a management interface for defining named workflows that govern how field tasks progress through stages. Pre-seed with common ISP workflows: Installation, Maintenance, Migration, and Troubleshooting.
- [ ] **Add workflow step configuration** -- Allow operators to define the steps within each workflow (e.g., Installation Workflow: Survey > Schedule > Deploy Equipment > Configure > Test > Sign-off), with required/optional designations per step.
- [ ] **Add workflow-to-service-order linking** -- Automatically apply the appropriate workflow when a service order is created based on the order type (new installation, plan change, migration, repair).
- [ ] **Add workflow status tracking** -- Display workflow progress visually (progress bar or step indicator) on service order detail pages so agents and customers can see where the task is in its lifecycle.

---

## 9.11 Scheduling Teams

**Splynx Features Observed:**
Splynx provides team management (Config > Scheduling > Teams) with named teams: Support, Fiber installation team, Lagos Team, and individual technician names (Brighten Daniel, Babatope Adewunmi, Isaac Absalom, Elisha Ajeh, Idris George). Each team has view, edit, and delete actions.

### Field Team Management

- [ ] **Add field team definitions** -- Create a management interface for defining field technician teams. Support both named teams (e.g., "Fiber Installation Team", "Radio Team") and individual technician entries.
- [ ] **Add team member assignment** -- Assign technicians to one or more teams, with skill-based categorization (fiber splicing, radio alignment, CPE installation, power systems).
- [ ] **Add team-based task routing** -- Route scheduled tasks to the appropriate team based on task type, location, and required skills. Support load-balancing across team members.
- [ ] **Add team capacity management** -- Track team member availability, current task load, and geographic coverage to optimize task assignment and scheduling.

---

## 9.12 Task Templates

**Splynx Features Observed:**
Splynx provides task templates (Config > Scheduling > Task templates) with 20+ ISP-specific templates: Radio Signal Confirmation, Addition of Fallback IP, Addition of Device to UNMS, Deployment of Devices to Client Site, Internet Status Confirmation from Client, Sending of Onboarding Email to Client, Minimal Power Confirmation (Fiber), Creation of Client on UNMS, RADIO (ALL), FIBER (ALL), Fiber Installation, Core Integration, Radio Installation, Fiber Migration, NGO Temp, Gov ITT, Gov Fin, Gov EOI. Each template has view, edit, and delete actions.

### Task Template Library

- [ ] **Add task template management** -- Create a library of reusable task templates for common field operations. Pre-seed with ISP-relevant templates: signal confirmation, IP configuration, device provisioning, equipment deployment, client onboarding, power verification, and NMS registration.
- [ ] **Add task template categories** -- Organize templates by technology type (Fiber, Radio, Power) and operation type (Installation, Maintenance, Migration, Troubleshooting) for quick filtering.
- [ ] **Add task template auto-population** -- When creating a new scheduled task, allow selection of a template that pre-fills the task description, checklist items, estimated duration, required skills, and equipment list.
- [ ] **Add composite task templates** -- Support templates that create multiple linked sub-tasks (e.g., "FIBER (ALL)" creates a sequence of fiber-specific tasks from survey through sign-off).

---

## 9.13 Checklist Templates

**Splynx Features Observed:**
Splynx provides checklist templates (Config > Scheduling > Checklist templates) with 29+ entries: Connect client, Disconnect client, Customer's repair, Equipment installation, Equipment removal, Equipment repairing, Signal Confirmation, Addition of Fallback IP, Addition of Device to UNMS, Deployment of Devices to Client Site, Internet Status Confirmation from Client, Sending of Onboarding Email to Client, Minimal Power Confirmation (Fiber), Creation of Client on UNMS, Fiber Cable Installation, FIBER(ALL), RADIO (ALL), L2 VPN Connection, Wireless Access Link Deployment, Customers Premise, Last-Mile, Core Integration, Fiber Installation, Radio Installation, BTS Maintenance, NGO Temp, Gov ITT, Gov Fin, Gov EOI. Each has view, edit, and delete actions.

### Checklist Management

- [ ] **Add checklist template management** -- Create a library of reusable checklists that can be attached to scheduled tasks and service orders. Each checklist contains ordered items that technicians must complete and mark off in the field.
- [ ] **Add ISP-specific checklist items** -- Pre-seed checklist templates with ISP field work items: connect/disconnect client, signal verification, equipment installation/removal/repair, cable installation, power confirmation, NMS registration, VPN setup, and customer sign-off.
- [ ] **Add checklist completion tracking** -- Track checklist completion percentage on task detail and list views. Require all mandatory checklist items to be completed before a task can be marked as finished.
- [ ] **Add checklist photo evidence** -- Allow technicians to attach photos to individual checklist items as proof of completion (e.g., photo of signal meter reading, photo of installed equipment, photo of cable routing).
- [ ] **Add checklist-to-workflow integration** -- Link checklist templates to workflow steps so that the appropriate checklist is automatically attached when a task reaches a specific workflow stage.

---

## 9.14 Project Types & Categories

**Splynx Features Observed:**
Splynx provides project type definitions (Config > Scheduling > Project types): Default type, Fiber, Radio, Power, and Business Development. Separately, project categories (Config > Scheduling > Project categories) include: Default category, New Installation, Troubleshooting, Migration, Retrieval, Fiber Maintenance, and Radio Maintenance. This two-dimensional classification (type x category) enables rich filtering and reporting.

### Project Classification

- [ ] **Add project type management** -- Define project types that represent the technology domain: Fiber, Radio, Power, Business Development, and a Default catch-all. Use types for team routing and skill matching.
- [ ] **Add project category management** -- Define project categories that represent the work activity: New Installation, Troubleshooting, Migration, Retrieval, Fiber Maintenance, Radio Maintenance. Use categories for reporting and SLA assignment.
- [ ] **Add two-dimensional project classification** -- Support filtering and reporting by both type (technology) and category (activity), enabling queries like "all Fiber Installation projects" or "all Troubleshooting tasks regardless of technology".
- [ ] **Add project type/category defaults on service orders** -- Automatically set project type and category when creating service orders based on the subscription type and order reason, reducing manual data entry.

---

## 9.15 Scheduling Notifications

**Splynx Features Observed:**
Splynx provides extensive scheduling notification configuration (Config > Scheduling > Notifications) organized into sections: Team Settings (enable notifications for teams), Watchers Notification (auto-add reporter to watchers), On Assign Notifications (Email + SMS with configurable templates), On Change Notifications (project change, partner change, related task change, related service change, priority change, status change, information change, "is scheduled" notification, "customer added/changed" notification), Comment Notifications (add/edit/delete comment), Attachment Notifications (add/delete attachment), Worklog Notifications, Checklist Notifications (add/delete/check/uncheck items), Reminder Notifications (send 3 hours before scheduled time via Email + SMS), and Digest Notifications (daily digest at configurable time).

### Scheduling Notification System

- [ ] **Add team-level scheduling notifications** -- Enable/disable notifications at the team level so all team members receive relevant task updates for their assigned group.
- [ ] **Add task assignment notifications** -- Send Email + SMS notifications when a task is assigned to a technician, using configurable message templates with task details, location, and scheduled time.
- [ ] **Add task change notifications** -- Send notifications on configurable events: project change, related task change, priority change, status change, scheduling change, and customer linkage changes. Allow individual toggle per event type.
- [ ] **Add task comment and attachment notifications** -- Notify watchers when comments are added/edited/deleted and when attachments are added/deleted on tasks, keeping the team informed of activity.
- [ ] **Add worklog notifications** -- Notify relevant parties when technicians log time against a task, providing visibility into field work progress.
- [ ] **Add checklist change notifications** -- Notify watchers when checklist items are added, completed, or unchecked, providing real-time progress updates on field work.
- [ ] **Add task reminder notifications** -- Send configurable reminders before scheduled task time (e.g., 3 hours before, 1 hour before, 30 minutes before) via Email + SMS to ensure technicians are prepared and en route.
- [ ] **Add daily task digest notifications** -- Send a daily digest email at a configurable time (e.g., 16:00) summarizing the day's completed, pending, and upcoming tasks for each team or individual.
- [ ] **Add task watcher auto-enrollment** -- Automatically add the task reporter (creator) to the watcher list, with an option to disable this behavior. Allow manual addition/removal of watchers.

---

## 9.16 Helpdesk-Scheduling Integration

**Splynx Features Observed:**
Multiple screenshots reveal deep integration between the helpdesk and scheduling modules. The ticket configuration references scheduling projects for auto-assignment, ticket types map to service actions that would generate scheduled tasks, and the organizational structure (ticket groups and scheduling teams) often mirrors each other.

### Cross-Module Integration

- [ ] **Add ticket-to-task conversion** -- Allow agents to create a scheduled field task directly from a helpdesk ticket with one click, pre-populating the task with ticket details, customer information, and location.
- [ ] **Add linked ticket/task visibility** -- When a ticket has an associated scheduled task (and vice versa), display a linked item panel showing status, assignee, and scheduled time on both the ticket detail and task detail views.
- [ ] **Add ticket resolution from task completion** -- When a linked scheduled task is completed, automatically prompt or auto-close the associated helpdesk ticket with a completion note summarizing the field work performed.
- [ ] **Add unified timeline view** -- On the customer detail page, show a combined timeline of helpdesk tickets and scheduled tasks, providing a complete service history for each subscriber.

---

## Priority Summary

### P0 -- Critical (Core Helpdesk Foundation)
These items are essential for basic helpdesk operations and should be implemented first:

| Count | Area |
|-------|------|
| 3 | Ticket status workflow and status-action linking (Section 9.2) |
| 3 | Ticket type taxonomy with ISP-specific categories (Section 9.3) |
| 2 | Ticket group management and agent assignment (Section 9.4) |
| 2 | Basic ticket email notifications (Section 9.5) |

### P1 -- High (Scheduling Foundation)
These items enable field service management alongside the helpdesk:

| Count | Area |
|-------|------|
| 4 | Scheduling workflow definitions (Section 9.10) |
| 3 | Field team management (Section 9.11) |
| 4 | Checklist template management (Section 9.13) |
| 3 | Task template library (Section 9.12) |

### P2 -- Medium (Automation & Integration)
These items improve efficiency through automation and cross-module integration:

| Count | Area |
|-------|------|
| 4 | Ticket automation rules engine (Section 9.9) |
| 4 | Helpdesk-scheduling integration (Section 9.16) |
| 4 | Canned response library (Section 9.6) |
| 4 | Email-to-ticket conversion (Section 9.8) |
| 4 | Project classification (Section 9.14) |

### P3 -- Low (Advanced Features & Polish)
These items enhance the system but are not blocking for core operations:

| Count | Area |
|-------|------|
| 4 | Customer-facing ticket widget (Section 9.7) |
| 9 | Scheduling notification system (Section 9.15) |
| 4 | Ticket notification templates and watcher system (Section 9.5) |
| 4 | Ticket auto-assignment and round-robin (Section 9.1) |

### Total Feature Improvements: 86 items across 16 subsections

### Implementation Approach

The helpdesk and scheduling modules represent a major new capability for DotMac Sub. The recommended implementation sequence is:

1. **Phase 1 -- Helpdesk Core**: Ticket model, statuses, types, groups, and basic CRUD operations
2. **Phase 2 -- Scheduling Core**: Task model, workflows, teams, templates, and checklist system
3. **Phase 3 -- Notifications**: Ticket and task notification pipelines with configurable templates
4. **Phase 4 -- Automation**: Ticket automation rules, email-to-ticket, and SLA management
5. **Phase 5 -- Integration**: Ticket-to-task linking, unified timeline, and public widget

### Data Model Considerations

New models required:
- `Ticket` -- Core ticket entity with status, type, priority, group, assignee
- `TicketStatus` -- Configurable status definitions with labels and workflow mappings
- `TicketType` -- ISP-specific ticket type taxonomy
- `TicketGroup` -- Team/group definitions with agent membership
- `TicketMessage` -- Threaded conversation on tickets (customer and agent messages)
- `TicketNote` -- Internal-only notes visible to agents
- `CannedResponse` -- Pre-written response templates
- `CannedResponseGroup` -- Organizational grouping for canned responses
- `TicketAutomationRule` -- Time-based escalation and status change rules
- `SchedulingWorkflow` -- Named workflow definitions with steps
- `SchedulingTeam` -- Field team definitions with member assignment
- `TaskTemplate` -- Reusable task template library
- `ChecklistTemplate` -- Reusable checklist definitions
- `ChecklistItem` -- Individual checklist items within templates
- `ProjectType` -- Technology-based classification
- `ProjectCategory` -- Activity-based classification
- `ScheduledTask` -- Field task entity linked to workflows, teams, and checklists
- `TaskChecklist` -- Instance of a checklist attached to a specific task
- `TaskChecklistItem` -- Individual checklist item completion tracking
- `TaskWorklog` -- Time logging for field tasks

### Technology Notes

- All list views should use HTMX for filtering and pagination
- Ticket conversation threads should use HTMX partial updates for real-time feel
- Scheduling calendar views can leverage Alpine.js for interactive calendar rendering
- Checklist completion should use HTMX inline toggles for instant feedback
- Email-to-ticket pipeline should be implemented as a Celery periodic task
- Ticket automation rules should run as a Celery beat scheduled task
- Notification delivery should leverage the existing `app/tasks/notifications.py` infrastructure
