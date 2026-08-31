# Upcoming Charges report

## Purpose

`/admin/reports/upcoming-charges` is an operator reminder worklist. It does not
create invoices, change service state, or send customer messages.

- **Postpaid** shows active collectible invoices due inside the configured
  lead window. Overdue rows remain eligible while the account still has a
  collectible postpaid service.
- **Prepaid** shows the latest exact `ServiceEntitlement` coverage boundary
  inside the configured lead window. The selected amount band applies to the
  subscription's contracted recurring `unit_price`; the displayed renewal
  amount is then resolved by the canonical batched prepaid charge owner and
  includes the applicable discount and tax treatment.
- Active `prepaid` or `overdue` enforcement locks keep recoverable suspended
  services on the worklist. Administrative, fraud, FUP, customer-hold, and
  system suspensions do not make an otherwise expired service collectible.
- A financially locked prepaid service with sufficient funding is retained as
  **Needs review**, even when already-funded rows are otherwise hidden. This
  catches stale enforcement rather than silently removing the customer.

Stopped, disabled, hidden, archived, canceled, and expired subscriptions are
not reminder candidates.

## Runtime shape

The page deliberately has separate Postpaid and Prepaid tabs. A request runs
only the selected mode's query.

1. An indexed SQL query selects and paginates bounded candidates.
2. Postpaid rows use persisted invoice facts directly.
3. Only the visible prepaid candidate page is passed to
   `resolve_prepaid_monthly_charges` and `prepaid_available_balances`; both are
   set-based owners. No per-row pricing or wallet query is performed.
4. The page size is capped at 50 (25 by default). The query requests one extra
   candidate to determine whether a next page exists; it does not run an
   unbounded total-count aggregate or load the full result set into Python.

Already-funded prepaid rows can be hidden after enrichment, so a page can
contain fewer visible rows than its candidate page. Pagination still advances
the stable candidate ordering and never repeats a record.

## Configuration

The Billing & Invoice Settings UI owns these fleet-wide defaults:

- `billing.upcoming_charges_postpaid_lead_days` (default `14`)
- `billing.upcoming_charges_prepaid_lead_days` (default `7`)
- `billing.upcoming_charges_prepaid_amount_bands` (default
  `50000-100000,100000-500000,500000-`)
- `billing.upcoming_charges_include_funded_prepaid_default` (default `false`)

Ranges are lower-inclusive and upper-exclusive. They must be ordered,
non-overlapping, non-negative, and only the final range may omit its maximum.
Choosing “All configured ranges” uses the union of those ranges, so plans below
the first minimum or inside a deliberate gap are excluded. The currency is the
configured prepaid enforcement currency.

These settings alter report selection only. They do not change billing,
collections, enforcement, or renewal behavior.
