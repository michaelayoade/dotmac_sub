# Admin revenue report date range

The finance operator uses `/admin/reports/revenue` to inspect collections,
invoicing, recurring revenue, and payments for an applied calendar period.
`financial.billing_reporting` owns the financial figures and payment cohort;
the web report service presents them. The audit owner records deliberate Apply,
Clear, and Export reads with the authenticated staff principal.

The page defaults to the last 30 inclusive UTC calendar days. Both Start date
and End date are required for a custom period, and the end must not precede the
start. The selected dates remain in the controls. The first viewport renders
the title and filter controls, then loads the four KPI cards, collections
trend, and recent payments in one asynchronous fragment. The activity panel
loads separately and refreshes every 20 seconds, including after Export.
Apply is the primary action; Clear and permission-gated Export are secondary.
The page and data fragment require `reports:billing:read`; CSV requires
`reports:billing:export`. There is no customer drill-down or destructive action.

Collections and the trend use active successful payments by `paid_at` in the
inclusive period. Growth compares with the preceding period of equal length.
Invoiced value uses non-void active invoices by `issued_at`. Outstanding is the
**current** balance on collectible invoices issued in the selected period;
it is not a historical balance as of the period end. Collection Rate divides
period collections by period invoiced value. Recurring Revenue uses the latest
available daily MRR snapshot at or before the end date and shows its as-of date;
missing snapshot evidence is unavailable, not zero. Recent Payments shows the
latest ten active successful payments within the range. The CSV exports every
active payment with a `paid_at` in the same range, including the same payment
statuses and columns as before, without a row cap. The CSV and recent-payment
panel differ in status scope by design.

The billing database is authoritative; no report cache or separate derived
store is introduced. Each page load reads fresh source facts. The MRR as-of
date signals snapshot freshness. Re-running the read is the idempotent rebuild
path, and finance operations owns investigation of a missing or stale snapshot.
An empty payment cohort shows a chart and payment empty state. Invalid dates
return a validation error. A missing MRR snapshot shows Unavailable. RBAC
controls the read and export endpoints, and the filter controls wrap on mobile.
