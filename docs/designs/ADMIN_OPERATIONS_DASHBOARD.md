# Admin Operations Dashboard

## Page contract

- **Audience:** authenticated operations, support, network, customer, and finance staff with at least one dashboard read permission.
- **Job:** identify urgent operational work, understand the latest customer/service/billing position, and move into an exact work queue.
- **Decision supported:** what needs attention now, what belongs to the current operator, and which customer, ticket, provisioning, billing, or network queue should be opened next.
- **Read owners:** `customer.dashboard_overview` for customer counts, `support.ticket_sla_clock` and `support.ticket_sla_reporting` for ticket/SLA state, network monitoring owners for device health, billing reporting for collections, audit for activity, and the ERP attendance adapter for the current operator's attendance state.
- **Freshness:** the shared dashboard snapshot carries its actual observation time. Attendance refreshes independently. Ticket performance carries its own observation time and degrades to an unavailable state if its query fails.
- **Primary action:** Add Customer. Attendance is a separate user-state control, not a second page-level business action.
- **First work surface:** Needs attention and My work. Four compact KPIs follow. No more than two equally weighted charts appear before Important activity.
- **Role projection:** Performance Trend is visible only to `admin` and `super_admin` roles. For every other authorized staff role, Tickets Performance and Important activity form the two-column desktop row so the hidden card leaves no empty region.
- **Personal work:** My work includes open tickets that mention the current staff user directly or mention any active service team containing that user's canonical Person membership.
- **Greeting:** the display-timezone morning/afternoon/evening label is used for the first successful login of the day. Two or more successful-login audit events on the same local calendar day switch the label to “Welcome.”

## Responsive projection

- Mobile uses one column, full-width controls, and preserves each item's identity, state, impact, and destination.
- KPI cards become two columns at the small breakpoint and four only at desktop width.
- Performance cards stack through tablet widths and become equal columns on wide desktop screens.
- Attendance and Add Customer stack on narrow screens with independent 44px minimum targets.
- The service-team selector remains a labeled native select and the chart retains a screen-reader summary.

## State and color

- Ticket series use both color and marker shape: breached SLA is rose/circle, close to SLA is amber/diamond, and newly created is blue/square.
- Light and dark modes use paired surface, border, foreground, hover, and focus states.
- Empty or failed chart reads render an explicit unavailable state; the page does not synthesize historical values.
