# Cutover Billing Reconciliation - July 2026

This note records the cutover balance identity used to repair the June 23/24
billing incidents and the invariants future cleanup scripts must preserve.

## Source Of Truth

For migrated Splynx customers, `subscribers.deposit` is a historical source
value, not a derived local ledger cache. It equals the Splynx billing mirror:

```text
deposit = sum(non-deleted splynx_billing_transactions credits)
        - sum(non-deleted splynx_billing_transactions debits)
```

The imported local ledger cannot independently reconstruct that value because
Splynx had already netted invoices, payments, voids, and deleted transaction
rows before cutover. Treat both `subscribers.deposit` and the
`splynx_billing_transactions` mirror as immutable audit evidence unless a
future migration explicitly re-imports the whole mirror and revalidates this
identity.

## Counterfactual Balance Identity

For a cutover-seeded account, the portal available balance should equal:

```text
target_available = subscribers.deposit
                 + sum(post-cutover succeeded payments)
                 - sum(post-cutover active non-void non-proforma invoice totals)
```

This intentionally excludes pre-cutover invoice state. Pre-cutover obligations
are already inside `subscribers.deposit` because Splynx posted ledger debits for
invoice charges whether they were paid or not.

Operational current available balance is:

```text
current_available = active null-invoice ledger credits
                  - active null-invoice ledger debits
                  - open invoice balances
```

The invariant is:

```text
current_available == target_available
```

Every cutover-era remediation must use this identity as the gate before moving
money or changing active ledger rows.

## Opening-Balance Construction Rows

Rows with memo `Prepaid opening balance @ cutover` are construction rows written
by the seeder to make the portal formula land on Splynx deposit truth. They are
not necessarily customer payments and they are not necessarily errors when they
are large debits.

The non-obvious case is a negative construction debit. It means the imported
null-invoice ledger residue exceeded the Splynx deposit and needed to be
cancelled so the local formula matched the source-of-truth deposit. Removing
that debit gives the customer unearned balance.

This is what happened on June 24:

1. The seeder wrote legitimate opening construction debits.
2. `reconcile_phantom_opening_debits` classified some debits as phantom using a
   non-negative-deposit heuristic that ignored imported residue.
3. A later cosmetic cleanup soft-deleted the debit/reversal pairs.
4. Customer balances were inflated until the July 3 counterfactual repair
   restored exact-match construction debits.

Do not deactivate opening construction rows to make the ledger display cleaner.
The durable UI fix is to fold cutover construction rows into an opening-balance
presentation line in the statement view.

## Applied Repairs

The safe repair gate was:

```text
current_available - target_available == inactive construction debits to restore
```

Only exact matches were restored. The July 3 phase-2 sweep restored 2,176
inactive opening construction debits, totaling NGN 202,040,970.40. Together
with three earlier manual restores, 2,179 accounts now have active construction
debits whose balances match the counterfactual identity to the cent.

Rows that did not pass the identity remain review-only. They must be bucketed by
gap cause before any further remediation.

## Regression Guard

`app.services.cutover_balance_audit.audit_cutover_balance_invariant` recomputes
the identity for all cutover-seeded accounts. The scheduled Celery task
`app.tasks.billing.audit_cutover_balance_invariant` runs it daily by default via
`cutover_balance_invariant_audit`.

Settings/env controls:

```text
billing.cutover_balance_audit_enabled
BILLING_CUTOVER_BALANCE_AUDIT_ENABLED

billing.cutover_balance_audit_interval_seconds
BILLING_CUTOVER_BALANCE_AUDIT_INTERVAL_SECONDS
```

The task is read-only. It logs an operator-visible error when drift exists and
returns samples ranked by absolute drift.

## Customer-Comms Tail

The ledger corrections made true debt visible again. That is accounting-correct
but customer-visible. Any status report should distinguish:

- Books corrected for exact-match cohorts.
- Operational blast radius handled only after affected customers have been
  notified and dunning/enforcement timing has been decided.

Known customer follow-up:

- Nigeria Custom Service Evolve 1: complaint was valid; corrected balance after
  the June 23 contra removal is NGN 188,337.33.
- Mr. Sheriff Lawanson: the June 23 contra and missing NGN 15,000 seed credit
  were fixed; his remaining debt is real and should be communicated using the
  corrected amount.
