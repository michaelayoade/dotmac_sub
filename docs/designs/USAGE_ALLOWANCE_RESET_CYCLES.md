# Usage Allowance Reset Cycles

## Decision

UsageAllowance is the catalogue authority for quota reset behavior:

- calendar_month keeps the UTC calendar-month bucket. This is the default for
  monthly unlimited/FUP products.
- renewal_cycle opens a bucket from the latest funded service entitlement.
  validity_days is required; current capped high-speed data products use 30.
- An early funded renewal of a renewal-cycle product starts a new validity
  interval at the payment instant. Existing live coverage does not defer that
  new capped cycle.

Migration 617 classifies existing capped high_speed_data allowances as
renewal_cycle with 30-day validity. All other allowances retain calendar_month.

## Rollover

Rollover is optional and lasts for exactly one additional cycle. Consumption
uses prior-cycle rollover first, then still-valid top-ups, then fresh base data.
Only unused fresh base data can become the next cycle's rollover. Prior
rollover cannot roll a second time. The amount is capped at one fresh cycle and
records its source bucket. A plan change or a gap expires rollover; top-ups
retain their own purchase validity.

## Open RADIUS sessions

A RADIUS session can span renewal. The renewal transaction creates the new
bucket and snapshots the session's cumulative counters. Metering subtracts that
immutable baseline, so old-cycle bytes are not inherited and no disconnect is
required to reset accounting.

## FUP interaction

FUP policy remains independent from billing allowance policy. Daily and weekly
windows are unchanged. A monthly FUP rule on a capped renewal-cycle plan uses
the quota bucket's exact interval for usage and cap_resets_at. Calendar products
continue to use the UTC calendar month.

## Ownership and transaction boundary

usage.quota_cycle_policy owns boundary, rollover, and counter-delta resolution.
financial.prepaid_service_renewals decides and funds the service interval, then
invokes quota initialization as a flush-only participant in the same
transaction. Metering persists the resolved bucket and recomputes usage from
absolute RADIUS counters.
