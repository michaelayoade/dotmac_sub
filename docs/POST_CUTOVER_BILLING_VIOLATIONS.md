# Retired: Post-Cutover Billing Violations

Status: retired for finance review.

This document used to describe the June 2026 line-level billing violation
worklist. It is intentionally no longer a finance source of truth.

Use this instead:

```text
docs/runbooks/cutover-billing-reconciliation-2026-07-03.md
```

The current finance review is based on reconstructed customer balances:

```text
Splynx cutover balance
+ payments received since cutover
- services consumed / charged since cutover
+ ordinary post-cutover adjustments
```

The old `billing_violations_*.csv` packet may still be useful as engineering
evidence for why a specific invoice line was created, but it must not drive
customer balance cleanup by itself. If there is disagreement, the reconstructed
customer statement wins.
