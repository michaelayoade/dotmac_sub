# Retired: Post-Cutover Fallback Artifacts

Status: retired for finance review.

This document used to describe the June 2026 imported-deposit fallback worklists.
Those worklists were diagnostic scaffolding during cutover hardening. They are
intentionally no longer a finance source of truth.

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

The old `post_cutover_*.csv` packets should not be regenerated for finance
decisions unless engineering explicitly needs them as provenance for a specific
legacy investigation. If there is disagreement, the reconstructed customer
statement wins.
