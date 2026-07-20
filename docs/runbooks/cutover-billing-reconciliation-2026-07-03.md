# Cutover Customer Balance Reconstruction

Status: **independent replay and current-production D12 containment check
completed on 2026-07-14; automatic correction remains suspended pending the
prepaid warning/grace policy decision, an independent-funding dry-run, and
finance review**.

## 2026-07-14 audit correction

The formula in this runbook remains the approved model. Its implementation did
not satisfy the formula:

- it substituted mutable `subscribers.deposit` for the authoritative Splynx
  cutoff transaction ledger; and
- it substituted current invoice/ledger rows for independently derived service
  consumption.

Those fields are outputs that cutover and remediation scripts may have changed.
They cannot validate themselves. The 2026-07-09 zero-difference result proves
only that current local state matched that local invariant after 88 corrections;
it does not independently prove the corrections or the customer balances.

Do not generate or apply another correction packet from
`app.services.cutover_balance_audit`; its target remains circular. The replacement
audit replays:

```text
authoritative Splynx cutoff net
+ proven post-cutover settlements, refunds and credit decisions
- service periods derived from cutoff services plus catalog/subscription changes
  and applied service extensions
+ provenance-backed manual adjustments
```

Current deposit, documents, allocations, ledger rows and enforcement state are
then comparison outputs. See
`docs/audits/BILLING_ALIGNMENT_RUN_2026-07-12.md` §7.

The accepted current-state pass ran against the explicitly named Sub production
host `selfcare.dotmac.io`. Funded-with-lock drift is zero. The remaining
population is 2,539 independently unfunded accounts without money locks; 2,533
are marked served, 2,166 have unrestricted FreeRADIUS authentication, and 492
have a recent open session. The deployed owner files match the merged F6/F7/F8
code. Enforcement is off because the active legacy database control is false,
while its deactivation policy is zero days. Do not enable it blindly: decide the
warning/grace policy first, produce an independent-funding dry-run that applies
the owner shields/health gates, then authorize the production control change as
a separate action.

This document replaces the older cutover audit and anomaly worklists for finance
review. The only finance balance review that should be used now is the customer
statement reconstruction:

```text
reconstructed balance =
  Splynx cutover balance
+ payments received since cutover
- services consumed / charged since cutover
+ ordinary post-cutover adjustments
```

The older post-cutover fallback, billing-violation, phantom-invoice, and
intermediate remediation documents were diagnostic worklists. They are not the
customer balance source of truth and should not be used for finance decisions.

## Cutover Boundaries

- Opening balance source: the final source Splynx position in the retained June
  29 snapshot. Its transaction ledger has no financial event after 2026-06-17
  and reconciles exactly to Splynx's own deposit. The June 16-17 overlap is
  therefore absorbed once, in the source baseline. `subscribers.deposit` is a
  comparison output.
- Native money facts from: `2026-06-18 00:00:00 UTC`.
- Service charges from: `2026-06-18 00:00:00 UTC`, derived from each source
  service's last charged paid-through period and charged amount, then adjusted by
  authoritative service/catalog decisions and applied service-extension entries.
- Ordinary adjustments are included only when they are real post-cutover
  account adjustments.
- Remediation-only adjustment memos are excluded from the reconstructed statement
  target.

## Current Snapshot

Generated from production on 2026-07-09. Retained below as historical execution
evidence; it is not an independently validated balance result.

```text
population: 15055 cutover-seeded customers
differences: 0 customers
overcredited: 0 customers / NGN 0.00
understated: 0 customers / NGN 0.00
statement transaction rows for difference cases: 0
```

The first reconstructed statement run found 88 customer balance differences:

```text
overcredited: 74 customers / NGN 4,730,371.96
understated: 14 customers / NGN 1,001,063.44
```

Those were corrected on 2026-07-09 with signed, manifest-checked adjustment
runs:

```text
understated customer credits applied: 14 / NGN 1,001,063.44
overcredited customer debits applied: 74 / NGN 4,730,371.96
```

The final invariant check after both correction runs returned `ok: true` and
`post_adjustment_drift_count: 0`.

Artifacts generated locally:

```text
scratchpad/cutover_reconstructed_statements_final/all_reconstructed_balances.csv
scratchpad/cutover_reconstructed_statements_final/drift_cases.csv
scratchpad/cutover_reconstructed_statements_final/drift_case_statement_transactions.csv
scratchpad/cutover_reconstructed_statements_final/manifest.json
scratchpad/cutover_reconstructed_balance_corrections_applied.json
scratchpad/cutover_reconstructed_balance_corrections_applied.csv
scratchpad/cutover_reconstructed_balance_overcredit_applied.json
scratchpad/cutover_reconstructed_balance_overcredit_applied.csv
```

These files contain customer financial data and must not be committed. Regenerate
and share them through the approved finance handoff location.

## Finance Rule

There is no licensed drift worklist from the 2026-07-09 correction runs. The
independent replay supersedes them. At the 2026-07-12 backup timestamp it found
589 persisted-deposit gaps / ₦73,041,254.69 across complete-replay accounts:
321 currently overcredited / ₦52,291,360.21 and 268 understated /
₦20,749,894.48. Another 93 accounts are incomplete and 12 active accounts lack a
source baseline. Generate no correction from the old exporter.

- `understated`: local books under-credit the customer compared with the
  reconstructed statement. These are the safest cleanup candidates because the
  correction is customer-favorable.
- `overcredited`: local books over-credit the customer compared with the
  reconstructed statement. Do not auto-debit from arithmetic alone. Finance must
  approve a debit, write-off, or accepted variance before the correction run.

## Regeneration

The exporter is read-only, but its target is circular. The replacement
reconstruction now lives in `scripts/one_off/billing_alignment_audit.py`; do not
use this older regeneration command for finance decisions unless its internals
are replaced with that independent source replay.

```bash
python -m scripts.one_off.cutover_reconstructed_balance export \
  --out-dir scratchpad/cutover_reconstructed_statements_current
```

The standing invariant service remains:

```text
app.services.cutover_balance_audit.audit_cutover_balance_invariant
```

That service exists to detect drift. The finance-facing review packet is the
statement reconstruction, not the older anomaly-specific audit documents.

## Correction Runs

Correction runs are dry-run by default.

```bash
python -m scripts.one_off.cutover_reconstructed_balance apply-corrections \
  --csv-out scratchpad/cutover_reconstructed_balance_corrections_current.csv \
  --json-out scratchpad/cutover_reconstructed_balance_corrections_current.json
```

By default, understated rows are prepared for application because they credit the
customer. Overcredited rows are held for finance approval. After finance approves
the overcredited worklist, add `--apply-overcredited`.

Add `--apply` only after the generated CSV/JSON has been reviewed:

```bash
python -m scripts.one_off.cutover_reconstructed_balance apply-corrections \
  --apply \
  --snapshot-date YYYY-MM-DD
```

Approved correction entries use the internal `Correction:` memo prefix. Customer
statements exclude those rows, so customers continue to see only real legacy
transactions, payments, service charges, credit notes, refunds, and approved
manual adjustments.
