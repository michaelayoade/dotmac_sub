# Post-cutover imported-deposit fallback — finance hand-off manifest

Companion to `docs/POST_CUTOVER_HARDENING.md` (billing data-hardening slice).

The CSVs below contain customer / service / financial state. They are
**reproducible artifacts, not source — they are git-ignored on purpose**
(`post_cutover_*.csv`). Regenerate them from the committed scripts; share the
files via the approved finance hand-off location, not the repo.

## How to regenerate (read-only; no DB writes)

```bash
# 1. Annotated audit of every account still on the account.deposit fallback
docker compose exec -T -e PYTHONPATH=/app app \
    python scripts/billing/audit_fallback_postpaid_ar.py
#    → writes (in-container /app):
#        post_cutover_fallback_postpaid_ar.csv   (annotated source of truth, 208 rows)
#        post_cutover_fallback_unwall_diff.csv    (un-wall signal flips, 29 rows)
#    copy out:  docker cp <app-container>:/app/post_cutover_*.csv .

# 2. Split the annotated source into per-classification finance worklists
poetry run python scripts/billing/split_fallback_worklists.py
```

The annotated CSV is the source of truth; the worklists are derived from it.

## Snapshot — generated 2026-06-17 (prod)

208 postpaid accounts still on the deposit fallback. Classification partitions
the 208 exactly (131 + 47 + 23 + 7).

| file | rows | owner / action |
| --- | ---: | --- |
| `post_cutover_fallback_postpaid_ar.csv` | 208 | **Audit source of truth** — annotated, all columns + classification. |
| `post_cutover_fallback_unwall_diff.csv` | 29 | Full un-wall signal-flip list (`deposit>=0` vs `not has_overdue_balance`). |
| `post_cutover_ar_safe_no_action.csv` | 131 | Reference only — 100 `ledger_reconciles` + 31 canceled/disabled. Fallback removal is a no-op. |
| `post_cutover_ar_diff_review.csv` | 47 | **Finance review** — `ledger_has_ar_but_differs`: AR populated but net ≠ deposit. |
| `post_cutover_missing_payments.csv` | 23 | **Highest priority** — `ledger_missing_payments`: AR overstates debt (payments not migrated). |
| `post_cutover_postpaid_credit_review.csv` | 7 | **Product/finance** — `deposit_credit_on_postpaid`: credit note / account credit / refund / void. |
| `post_cutover_unwall_risky.csv` | 23 | **Priority service-impact queue** — active/blocked accounts the proposed un-wall rule would wrongly restore. |

## SHA256 (snapshot integrity — 2026-06-17)

Hashes pin this generation. Re-running against changed prod data yields new
hashes; that is expected. Use these to verify the file finance received matches
what was generated.

```
5dee8f046540f960e8a23912168099b38acf6d8d7f14b1e3547e38c1931f6d73  post_cutover_ar_safe_no_action.csv
5d558ae308cff424a56db4c45714424bc89ef4cb1f43085980000e6d04d606a6  post_cutover_ar_diff_review.csv
66ed07b2dcb2fec591fe09804040657a910a51622caee7d17889455de981444e  post_cutover_missing_payments.csv
5996619dc416b0fecbd9eb6d8750a8f359d6ca2ede227e9dc4b65d3a80eb8a09  post_cutover_postpaid_credit_review.csv
759b54f6f7707359a9f2ed2f2a10b35ba1cd1108f4d93de9e38f9c0fa2757820  post_cutover_unwall_risky.csv
ec077b80cf9bb22154db6d63df5d18b363d874d9cfaba95f6e135f8fd3293865  post_cutover_fallback_postpaid_ar.csv
d0d87db1c272f1585971328a91341c5171207275ec44d96bdbb0b11718b358ec  post_cutover_fallback_unwall_diff.csv
```

## Sequence (no runtime AR-trust gate)

1. Use the split CSVs as finance work queues.
2. Resolve the 70 (`diff_review` + `missing_payments`) and the 7 credit accounts
   by **correcting local ledger/AR data or writing explicit adjudication
   entries** — not a runtime flag.
3. Keep the `account.deposit` fallback as the single safety **until
   `post_cutover_unwall_risky.csv` is closed**.
4. Then remove the fallback **globally** and add the guard: *no billing path
   reads `account.deposit` as available balance.*
