# Customer retention metrics

Status: implementation contract

The `/admin/customer-retention` page is a read-only report for billing operators.
Its period controls default to the latest 30 inclusive Africa/Lagos calendar
days. Both dates are required for a custom period. Apply reloads the page with
the dates in the URL; Clear restores the default. The shell renders first and
loads the period metrics and current billing-risk queue in separate requests.
Each request has an independent loading and error state.

The registered customer-retention report owner accepts a typed
`CustomerRetentionMetricsQuery` and returns a typed outcome. Churn uses
`subscriber_growth.churn_summary` and therefore has the same visible
subscriber population, unique churn event count, and historical fallback as
the churn report. Recovery reads only provenance-graded, trusted
`SubscriptionLifecycleEvent` transitions, using the same Lagos day bounds.
Recovered Customers counts distinct subscribers transitioning from suspended,
paused, or disabled to active within the period. The eligible recovery cohort
counts distinct subscribers with an inactive subscription at the period start,
one that became inactive during the period, or one that recovered during the
period. Recovery Rate is recovered divided by eligible; a zero denominator
shows N/A. Every count is computed in SQL and a subscriber is counted once.

The three metric cards are period based. Tracked Accounts, Revenue At Risk,
segment focus, search, queue ordering, pagination, profile links, and the
existing search-scoped queue CSV remain current-state billing-risk projections.
The CSV is explicitly labeled as a current queue export. The metrics are not
presented as campaign attribution or outreach outcomes.

Freshness: each fragment queries committed facts on demand. There is no
authoritative report cache. A failed fragment read stays visibly unavailable;
it does not substitute zero or stale numbers. Historical transitions before
trusted lifecycle evidence was introduced may be incomplete, so the metrics
fragment states that limitation and the churn fallback explicitly. Divergence
from the canonical churn report or raw trusted lifecycle counts is drift;
rerunning the typed query is the idempotent rebuild path. Customer lifecycle
and evidence owners repair their respective authoritative inputs.

The page uses `reports:billing:read`; queue CSV requires
`reports:billing:export`. It contains no write, bulk, or destructive action.
The profile drill-down is `/admin/customer-retention/{customer_id}`.
Desktop metrics occupy three cards and the queue remains a table; the cards
stack on narrow screens and the table retains its responsive behavior.
