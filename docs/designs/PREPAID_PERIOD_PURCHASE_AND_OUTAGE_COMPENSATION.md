# Prepaid period purchase and outage compensation

## Decisions

- A customer may buy 1–12 monthly service periods for one prepaid subscription.
- The quote starts at the current funded coverage tail. Each calendar period is
  priced and VAT-rounded independently; the checkout total is the sum of those
  immutable period totals.
- Any open invoice balance blocks a future-period purchase. An open confirmed
  customer outage also blocks new checkout initiation.
- Provider confirmation creates one invoice, one allocation, and one service
  entitlement per quoted period in one owner transaction. The selected payment
  may fund only those invoices; it must never enter oldest-invoice allocation.
- Cancellation is scheduled at the final paid-through boundary. Purchased
  periods are not prorated or automatically refunded, and plan changes wait for
  that boundary.
- Base subscription price is the only purchasable recurring charge. The owner
  fails closed if an active recurring add-on is present.

## Ownership

`financial.prepaid_period_purchases` is the sole writer of purchase headers,
period rows, purchase invoices, their selected-payment allocations, resulting
entitlements, and the final subscription billing anchor. Gateway verification
records confirmed cash, then delegates the complete settlement consequence to
that owner in the same transaction.

`financial.outage_compensation` is the sole writer of outage compensation
decisions and zero-value compensation entitlements. It consumes only finalized
`CustomerOutageInterval` evidence. It does not change subscription status and
does not rewrite purchased invoices, VAT, or service periods.

## Outage policy

- The default minimum eligible outage is six hours and is editable as
  `billing.outage_compensation_min_hours`.
- Durations are calculated in exact seconds. Overlapping finalized intervals
  are unioned before qualification.
- Compensation is capped to the intersection of outage time and already funded
  entitlement coverage for the affected subscription.
- A qualifying decision appends one zero-value entitlement to the then-current
  funded tail. A consumed interval cannot be reused by another decision.
- Resolved/discarded outage events trigger the compensation owner after the
  downtime ledger commits. Per-subscription owner-output receipts make event
  replay an exact no-op; no whole-customer financial sweep is introduced.
- Pending `next_cycle` cancellation/expiry schedules that target the old funded
  tail are rebased atomically to the compensated tail. Explicit calendar-date
  schedules are preserved as deliberate customer/operator decisions.
- The customer service page projects an active network interruption without
  mutating subscription lifecycle status. Checkout remains blocked by the
  open outage interval until recovery is finalized.
- Planned-maintenance exclusions and ambiguous evidence are recorded as
  explicit decisions; they are never silently discarded.

## Rollout

Both `billing.prepaid_period_purchase_enabled` and
`billing.outage_compensation_enabled` default to false. Schema deployment and
historical overlap/reconciliation checks precede enabling either writer.
