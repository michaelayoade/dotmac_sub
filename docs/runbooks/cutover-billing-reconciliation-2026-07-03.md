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
```

Payments are counted from `2026-06-16 00:00:00 UTC` by `payments.created_at`.
Invoices and ordinary manual adjustments are counted from
`2026-06-16 09:08:00 UTC`, after the opening-balance seed handoff. Remediation
memos (`Reversal of phantom%`, `Reversal of prepaid opening%`, `Correction:%`)
are excluded from the adjustment target and reported separately.

Opening rows with memo `Prepaid opening balance @ cutover` are construction rows
that make the local balance formula land on Splynx deposit truth. Do not
deactivate them for display cleanup; fold them into a statement opening-balance
presentation instead.

## Applied State

The June 24 phantom-opening reversal repair restored exact construction rows
only when the counterfactual invariant proved the correction. A later drift
queue pass applied 10 more exact corrections:

```text
missing unallocated payment credits: 3 accounts / NGN 56,437.00
opening construction restores: 4 accounts / NGN 850,000.00
opening construction restore + missing payment credit: 3 accounts
```

After that apply, the scheduled audit baseline is:

```text
population: 15055 cutover-seeded accounts
drift_count: 85
overcredited: 40 accounts / NGN 4,517,991.02
understated: 45 accounts / NGN 2,176,340.34
post-cutover adjustments: 17 entries / NGN -164,505.79
excluded remediation adjustments: 2 entries / NGN -57,500.00
```

Remaining buckets:

```text
review_inactive_pair_residual: 37 accounts / NGN 4,194,658.28 absolute drift
review_inactive_seed_without_pair: 16 accounts / NGN 880,014.12 absolute drift
review_unclassified: 32 accounts / NGN 1,619,658.96 absolute drift
```

The scheduled guard is `app.tasks.billing.audit_cutover_balance_invariant`,
registered as `cutover_balance_invariant_audit` every 86,400 seconds.
