# Customer Report page contract

Screen: `/admin/reports/customers` (alias `/admin/reports/subscribers`). Audience:
staff with `customer:read`. The page supports comparing a date- and status-scoped
customer cohort, its usage, signup trend, plan and region distribution, and
report interactions.

## Read ownership and scope

- `subscriber.growth_reports` owns the signup series. `CustomerGrowthQuery`
  carries an exclusive UTC end and optional current account status. The result
  has signup counts per bucket and a cumulative count starting with the cohort
  created before the selected window. The chart uses daily buckets for up to
  45 days, weekly buckets for up to 180 days, and calendar-month groups beyond
  that, capped at roughly 24 buckets for long windows.
  A current-status filter is applied consistently to the starting population
  and every signup bucket; it is not a historical status timeline.
- `customer.usage_summary` owns the per-customer usage-window aggregate. The
  selected dates are inclusive calendar dates, resolved to a half-open UTC
  interval. Native RADIUS session octets take precedence over imported daily
  usage for the same customer and day. Where neither has a matching day, a
  retained bandwidth sample provides a labeled estimate. A missing observation
  is distinct from a recorded zero. RADIUS session counters are attributed to
  the session-start day because arbitrary intra-session day splits are not
  stored. An end-only filter uses a 30-day window ending on the selected day.
  The page headline aggregates the full filtered cohort before
  pagination, matching the customer rows and export. The CSV carries each
  customer's usage source; a row with no observation displays a dash rather
  than a measured zero. The displayed period comes from the resolver, never a
  template fallback.
- `app.services.web_reports` composes the report. The initial typed overview
  query reads bounded KPI counts. The detailed cohort query runs only when the
  lazy report section is revealed. Both paths use the same KPI contract.
- `observability.audit_log` owns request audit evidence. Successful customer
  report views and exports are selected from persisted audit events by path;
  the page labels a view with filters as a filter activity. The fragment request
  is transport only and does not create an activity. Other GET paths retain
  the configured audit policy.

## Page projection

The first response contains the header, filters, and four KPI cards. The
secondary report section loads with an HTMX `revealed once` request and has a
manual link if JavaScript is unavailable. It displays Period Usage and Usage
Window at 24px, Customer Growth Trend, Active Customers by Plan, Regional
Breakdown, Matching Customers, By Status, Recent Sign-ups, and Reports Activity
in that order. The plan and region cards share a desktop row and stack on
mobile. The By Status contract and presentation remain as they were.

The growth chart has separate axes for cumulative customers and new signups.
If the selected scope has no signups, it states that explicitly instead of
showing a flat line. The filter and export URLs preserve the selected dates
and status. Detail loading must not prevent the initial filter and KPI response
from rendering.

## Validation

Focused tests cover filtered growth buckets, overview without detailed row
loading, recorded-byte priority and sample fallback, period usage parity, lazy
section order, and report audit selection.
Browser checks cover fragment loading, chart initialization, export, keyboard
navigation, and desktop/mobile layout. The report labels the session-start-day
attribution rather than claiming exact intra-session day slicing.
