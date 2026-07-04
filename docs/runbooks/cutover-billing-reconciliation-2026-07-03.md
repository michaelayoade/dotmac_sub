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
memos (`Reversal of phantom%`, `Reversal of prepaid opening%`, `Correction:%`)
are excluded from the adjustment target and reported separately.

Some prepaid renewals write a local ledger debit with `source='invoice'` and
`invoice_id = NULL` instead of an `invoices` table row. Those rows are real
post-cutover charges, so the audit subtracts them from the target and includes
them in the reported post-cutover invoice total.

Opening rows with memo `Prepaid opening balance @ cutover` are construction rows
that make the local balance formula land on Splynx deposit truth. Do not
deactivate them for display cleanup; fold them into a statement opening-balance
presentation instead.

## Applied State

The June 24 phantom-opening reversal repair restored exact construction rows
only when the counterfactual invariant proved the correction. Later drift queue
passes applied 19 more exact corrections:

```text
missing unallocated payment credits: 3 accounts / NGN 56,437.00
opening construction restores: 4 accounts / NGN 850,000.00
opening construction restore + missing payment credit: 3 accounts
ledger-charge-aware opening construction restores: 9 accounts / NGN 1,096,437.00
```

After that apply, the scheduled audit baseline is:

```text
population: 15055 cutover-seeded accounts
drift_count: 60
overcredited: 33 accounts / NGN 3,778,491.02
understated: 27 accounts / NGN 1,444,839.84
post-cutover adjustments: 18 entries / NGN -198,449.83
target adjustments: 16 entries / NGN -140,949.83
excluded remediation adjustments: 2 entries / NGN -57,500.00
```

Remaining phase-2 pair buckets:

```text
inactive_pair_balanced_review: 3 accounts / NGN 544,195.44 inactive originals
manual_review: 22 accounts / NGN 2,386,742.41 inactive originals
manual_understated_review: 13 accounts / NGN 743,548.62 inactive originals
review_exact_with_post_adjustments: 1 account / NGN 306,250.00 inactive originals
```

The scheduled guard is `app.tasks.billing.audit_cutover_balance_invariant`,
registered as `cutover_balance_invariant_audit` every 86,400 seconds.
