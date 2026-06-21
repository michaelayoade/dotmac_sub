# Billing-integrity violations — finance decision packet (manifest)

Track 2 of the post-cutover hardening (see `docs/POST_CUTOVER_HARDENING.md`).
The `billing_integrity_audit` launch-blocking gauges `billing_disabled_service_lines`
and `billing_duplicate_subscription_period_lines` found violations; this packet
is the per-line worklist for **finance** to disposition.

**READ-ONLY. No money records are mutated.** No credit notes, no voids — this
produces a decision packet only. The CSVs contain customer/financial PII and are
**git-ignored** (`billing_violations_*.csv`); regenerate from the committed
script and share via the approved finance hand-off location.

## How to regenerate

```bash
docker compose exec -T -e PYTHONPATH=/app app \
    python scripts/billing/billing_violation_worklist.py
#  → writes (in-container /app): billing_violations_annotated.csv (source of truth)
#    + the four per-disposition splits below.
#  copy out:  docker cp <app-container>:/app/billing_violations_*.csv .
```

## Snapshot — generated 2026-06-18 (prod)

163 offending invoice lines: **52** `disabled_service` (across 22 subscriptions)
+ **111** `duplicate_period` (across 51 groups). Total line amount across findings
**NGN 4,675,270.83**; the `credit_or_void_required` slice is **NGN 1,140,270.83**.

| file | rows | owner / action |
| --- | ---: | --- |
| `billing_violations_annotated.csv` | 163 | **Audit source of truth** — all findings, every column + proposed disposition. |
| `billing_violations_credit_or_void.csv` | 52 | **Finance** — disabled-service lines billing a period after cancellation. Credit note if the invoice was paid; void the line if unpaid. |
| `billing_violations_duplicate_review.csv` | 106 | **Finance** — same sub/period/description billed >1×. Confirm the duplicate, keep one line, void the rest. |
| `billing_violations_valid_historical.csv` | 5 | No action — duplicate lines on already-void invoices. |
| `billing_violations_manual_review.csv` | 0 | Ambiguous dates/status (none in this snapshot). |

Disposition is **conservative by default**: disabled-service post-cancellation →
`credit_or_void_required`; same sub/period/description → `duplicate_review`;
anything with missing period/end dates → `manual_finance_review`.

## Columns

`finding_type, invoice_id, invoice_number, invoice_status, invoice_line_id,
subscription_id, subscriber_id, splynx_customer_id, customer_name, service_status,
subscriber_status, billing_period_start, billing_period_end, line_description,
line_amount, invoice_total, invoice_balance_due, created_at, canceled_or_end_at,
duplicate_group_key, duplicate_group_count, proposed_disposition, reason`

## SHA256 (snapshot integrity — 2026-06-18)

Hashes pin this generation; re-running against changed prod data yields new
hashes (expected). Verify the file finance received matches what was generated.

```
4d07788e7e20fa76ef4e0e9a77f3eb68a0a7356c92596a35396ba9db341e44a8  billing_violations_annotated.csv
a02f1b1c3458acf26b7960e958bcdcd74aafab39ac4abad7f17689208c8754cd  billing_violations_credit_or_void.csv
706ea8e27389429d4e4edbf787b4c6a43b334abe2c6d1bee7df58e6811fbc893  billing_violations_duplicate_review.csv
f3fa5449e56aaa64fe7f8ffb2f5ea81f443310ef82c7be0a3f3d60cc4bb81518  billing_violations_valid_historical.csv
2f7c316c5a1fd95ad8dbc5808c0524f9fb1301ff2eba2c961e10d05ffd3928d4  billing_violations_manual_review.csv
```

## Rule

No credit note or void is written until finance signs off the disposition per
row. Billing automation stays off until the launch-blocking gauges are zero.
