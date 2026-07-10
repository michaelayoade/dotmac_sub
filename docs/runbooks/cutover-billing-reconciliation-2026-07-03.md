# Cutover Customer Balance Reconstruction

Status: current finance source of truth as of 2026-07-09.

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

- Opening balance source: `subscribers.deposit`, the Splynx mirror net balance at
  cutover.
- Payments from: `2026-06-16 00:00:00 UTC`.
- Service charges from: `2026-06-16 09:08:00 UTC`, after the opening-balance seed
  handoff.
- Ordinary adjustments are included only when they are real post-cutover
  account adjustments.
- Remediation-only adjustment memos are excluded from the reconstructed statement
  target.

## Current Snapshot

Generated from production on 2026-07-09.

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

There is no active drift worklist after the 2026-07-09 correction runs. If a
future regeneration produces rows in `drift_cases.csv`, use that file as the
worklist.

- `understated`: local books under-credit the customer compared with the
  reconstructed statement. These are the safest cleanup candidates because the
  correction is customer-favorable.
- `overcredited`: local books over-credit the customer compared with the
  reconstructed statement. Do not auto-debit from arithmetic alone. Finance must
  approve a debit, write-off, or accepted variance before the correction run.

## Regeneration

The exported packet is read-only. It should be regenerated from production when
finance asks for a fresh snapshot.

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
