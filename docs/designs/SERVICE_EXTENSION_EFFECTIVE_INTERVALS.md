# Service Extension Effective Intervals

## Decision

`financial.service_extensions` owns the lifecycle and exact service interval
for outage-compensation extensions. For each affected subscription, the owner
calculates:

```text
grant_starts_at = max(previous_next_billing_at, applied_at)
grant_ends_at = grant_starts_at + extension days
next_billing_at = grant_ends_at
```

This preserves additive behavior for a current or future billing anchor. When
the anchor is already expired, compensation begins when the operator applies
it, so none of the granted days are consumed in the past.

An absent billing anchor is ambiguous and remains ineligible. The owner records
it as skipped and does not invent a date.

## Authoritative record and projection

Each applied `ServiceExtensionEntry` records:

- `previous_next_billing_at`: the anchor observed before the command;
- `grant_starts_at` and `grant_ends_at`: the immutable exact service grant;
- `anchor_basis`: `existing_billing_anchor`, `application_time`, or the
  historical backfill value `legacy_previous_anchor`;
- `new_next_billing_at`: the billing-anchor projection written atomically with
  the grant.

The exact grant interval is authoritative. `Subscription.next_billing_at` is a
projection and must not be used to reconstruct or enlarge the grant.

## Consumers

The following consumers use the same half-open interval
`[grant_starts_at, grant_ends_at)`:

- prepaid service-coverage resolution;
- prepaid coverage reconciliation;
- billing-enforcement shielding;
- customer events and notifications;
- CRM responses and the admin preview/detail page.

No consumer may derive a second interval from entry creation time, the outage
window, or `previous_next_billing_at + days`.

## Transaction, locking, and retry

Create, apply, and cancel enter the `financial.service_extensions` owner
command boundary once. Apply and cancel lock the extension row before checking
the pending transition. The unique `(extension_id, subscription_id)` entry
identity prevents a concurrent command from recording the same grant twice.
Subscription restoration, audit staging, and event staging participate in the
same transaction and do not commit independently.

Applying or canceling an extension that is no longer pending fails closed.
Subscriptions without a billing anchor are skipped with durable batch counts.

## Historical migration

Existing rows are backfilled as:

```text
grant_starts_at = previous_next_billing_at
grant_ends_at = new_next_billing_at
anchor_basis = legacy_previous_anchor
```

This records the historical decision exactly as it occurred. The migration
does not move old billing dates or retroactively grant new service.

## Drift and repair

For a newly applied entry, `new_next_billing_at` and `grant_ends_at` must match.
The subscription anchor is expected to equal or exceed the latest valid grant
end after later billing activity. A grant with missing or reversed boundaries,
or an anchor behind its latest grant end, is drift and must be reconciled by
`financial.service_extensions`; read paths must not silently repair it.
