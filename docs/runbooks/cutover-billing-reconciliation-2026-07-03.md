# Cutover Billing Reconciliation - July 2026

`subscribers.deposit` is historical Splynx truth for migrated customers:

```text
deposit = sum(non-deleted Splynx mirror credits)
        - sum(non-deleted Splynx mirror debits)
```

For cutover-seeded accounts, the local portal balance should satisfy:

```text
current_available == subscribers.deposit
                   + post-cutover succeeded payments
                   + ordinary post-cutover null-invoice adjustments
                   - post-cutover active non-proforma invoice totals
                   - post-cutover ledger-only invoice charges
```

Payments are counted from `2026-06-16 00:00:00 UTC` by `payments.created_at`.
Invoices and ordinary manual adjustments are counted from
`2026-06-16 09:08:00 UTC`, after the opening-balance seed handoff. Remediation
memos (`Reversal of phantom%`, `Reversal of prepaid opening%`, `Correction:%`,
`Partial cutover opening balance construction adjustment%`,
`Data repair 2026-06-29:%`, `Validated account credit consumed%`) are excluded
from the adjustment target and reported separately.

Some prepaid renewals write a local ledger debit with `source='invoice'` and
`invoice_id = NULL` instead of an `invoices` table row. Those rows are real
post-cutover charges, so the audit subtracts them from the target and includes
them in the reported post-cutover invoice total.

Opening rows with memo `Prepaid opening balance @ cutover` are construction rows
that make the local balance formula land on Splynx deposit truth. Do not
deactivate them for display cleanup; fold them into a statement opening-balance
presentation instead.

## Variance Registry

The daily audit alarms on unregistered residual drift, not on a hand-maintained
numeric baseline. Known, reviewed differences belong in
`app/services/cutover_balance_variance_registry.json`.

Registry rules:

```text
status = candidate  -> documented for review, still alarms
status = accepted   -> subtract expected_drift from that account's raw drift
```

`expected_drift` uses the audit sign convention:

```text
drift = current_available - target_available
positive: local books over-credit the customer
negative: local books understate the customer
```

An accepted variance must have a reason and should only be added after the
source-of-truth difference has been verified, for example a deliberate write-off
or collections-side reconciliation that intentionally differs from Splynx mirror
truth. Stale accepted entries are reported separately and keep the audit non-OK.

## Applied State

The June 24 phantom-opening reversal repair restored exact construction rows
only when the counterfactual invariant proved the correction. Later drift queue
passes applied only invariant-proven corrections:

```text
missing unallocated payment credits: 3 accounts / NGN 56,437.00
opening construction restores: 4 accounts / NGN 850,000.00
opening construction restore + missing payment credit: 3 accounts
ledger-charge-aware opening construction restores: 9 accounts / NGN 1,096,437.00
post-merge exact opening/seed restores: 14 rows / NGN 862,611.97
post-merge missing payment credits: 4 accounts / NGN 75,251.00
partial opening-construction debit adjustments: 11 accounts / NGN 1,504,277.03
adjustment-aware exact opening construction restore: 1 account / NGN 306,250.00
mirror-backed seed construction credits: 2 accounts / NGN 980,916.67
```

After the mirror-evidence pass, adjustment-aware exact restore, and
mirror-backed seed credits, before registering accepted variances, the scheduled
audit reports:

```text
population: 15055 cutover-seeded accounts
raw_drift_count: 28
unregistered drift_count: 28
overcredited: 9 accounts / NGN 689,184.50
understated: 19 accounts / NGN 403,014.15
post-cutover adjustments: 29 entries / NGN -1,702,726.86
target adjustments: 13 entries / NGN 298,501.71
excluded remediation adjustments: 16 entries / NGN -2,001,228.57
```

Historical baseline log:

```text
2026-07-04 post ledger-charge refinement: 60 drift rows
2026-07-04 post seed/payment tail fixes: 43 drift rows
2026-07-04 post partial construction adjustments: 32 drift rows
2026-07-04 post mirror-evidence adjustment exclusions: 30 drift rows
2026-07-04 post mirror-backed seed credits: 28 drift rows
```

The scheduled guard is `app.tasks.billing.audit_cutover_balance_invariant`,
registered as `cutover_balance_invariant_audit` every 86,400 seconds.

## Funded Inactive Exposure

Positive balances on inactive accounts are a standing liability report, not a
cutover drift variance. The scheduled read-only task
`app.tasks.billing.audit_funded_inactive_exposure` reports inactive accounts
(`blocked`, `disabled`, `suspended`) whose portal available balance is positive,
using the same customer-facing balance formula:

```text
available = active null-invoice ledger credits
          - active null-invoice ledger debits
          - active open invoice balances
```

Policy:

```text
blocked   -> retention/win-back queue; customer may return with value intact
disabled  -> refund/disposition review
suspended -> account review; do not leave positive value buried under suspension
```

The task is registered as `funded_inactive_exposure_audit` every 2,592,000
seconds by default. It logs disabled/suspended funded exposure at ERROR level
and includes the largest accounts as samples for ops review.
