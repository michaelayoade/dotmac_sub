# Admin dashboards / reports / alerts — UX-polish & operator-control audit

**Date:** 2026-06-29
**Method:** single-agent read-only review of admin dashboard, reports, and alerts
(routes/services/templates).
**Status:** audit only. Part of the remaining-module audit series.

## What this audit is

Two tracks (definition in `NETWORKING_UX_POLISH_AUDIT.md`): **POLISH** and
**CONTROL**. This cluster's signature is **fabricated / empty data presented as
real** — placeholder KPIs, charts that can only show zeros, a CSV that emits only a
header. For a reporting surface that's worse than missing data, because operators
make decisions on it.

## Acceptance criteria (reports/dashboards-specific)

1. Every number shown is computed from data — no hardcoded growth %, no
   `total * 0.85`, no integer-division "reasons".
2. Every chart/export either has real data or isn't shown; no permanently-zero
   chart, no header-only CSV.
3. Aggregates are computed in SQL, not from a capped list — totals don't silently
   understate past the row cap.
4. Every "live/real-time" surface shows "data as of <ts>" or is relabeled.
5. Operational thresholds (alert backlogs, signal cutoffs) are configurable and
   single-sourced.

## Cross-cutting themes

### POLISH

**P-A. Fabricated / empty data shown as real (the signature).**
- Fabricated KPIs: `revenue_growth=12.5`, `subscriber_growth=8.3`,
  `recurring_revenue = total_revenue * 0.85`, `churn_reasons` via `cancelled//3,//4`
  (`app/services/web_reports.py:307,418,304,507-512`)
- Revenue/Churn "Trend" charts render `|default({Jan-Jun zeros})`; routes never pass
  the data → permanently flat-zero (`templates/admin/reports/revenue.html:51`, `churn.html:78`)
- Technician CSV export iterates `technician_stats=[]` (never populated) → header
  row only (`web_reports.py:757,772`)
- MRR From/To date inputs post `date_from`/`date_to` but the route only accepts
  `year` → filter does nothing (`templates/admin/reports/mrr.html:43-44`)

**P-B. Capped-aggregate understatement.** Revenue/subscribers/churn load `limit=1000`
and aggregate in Python, so Total Revenue / collection_rate / churn % silently
understate once the base exceeds 1000 (it's in the thousands) (`web_reports.py:251,274,366,475`).
→ aggregate in SQL.

**P-C. Freshness / label correctness.**
- "Real-time monitoring" header + 180s `hx-get` but context cached 60-180s and **no
  "data as of" stamp** anywhere (`templates/admin/dashboard/index.html:19`, `_stats.html:40`)
- Bandwidth "Avg Download" bound to `rx_bps`, but the project convention is
  `rx_bps = upload` → labels inverted (`templates/admin/reports/bandwidth.html:38,132`)

**P-D. Pagination missing.** Alerts inbox accepts `page`/`per_page`, shows `total`,
but renders no controls — anything past the first 25 is unreachable
(`templates/admin/alerts/index.html:51-119`).

### CONTROL

**C-1. Operational thresholds hardcoded.**
- Alert triggers: Celery reserved `>100`, queue `>500`, long-running `>30m`, Splynx
  sync healthy `<7200s` (`app/services/admin_alerts.py:424,440,411`, `web_admin_dashboard.py:542`)
- ONT low-signal `< -25 dBm` + "offline > 5" hardcoded here **and duplicated** on
  the ONT diagnostics pages (`web_admin_dashboard.py:209,702`) — same `-25` as the
  networking audit's `zabbix_ont_status.py` → single `ont_low_signal_dbm` setting

**C-2. Row / export / window caps hardcoded.** Report row caps (`limit=200`, top-100,
chart top-20) and CSV cap (`limit=5000`) (`web_reports_extended.py:147,233,283`,
`web_reports.py:326,431,524`); default window `days=30`, export dropdown only
30/90/365, network export `hours` has no UI control (`app/web/admin/reports.py:365,539`).

## Priority

| Tier | Items |
|------|-------|
| **P0** | Fabricated finance/retention KPIs shown as real (P-A); permanently-zero trend charts; header-only technician CSV; dead MRR date filter — all "lying to the operator" |
| **P1** | Capped-aggregate understatement of totals (P-B); "data as of" + soften "Real-time" (P-C); bandwidth rx/tx label inversion (P-C); alerts pagination (P-D); alert thresholds → settings + ONT-signal single-source (C-1) |
| **P2** | report row/export caps + windows as settings/options with truncation footers (C-2) |

## Appendix — full findings
- [POLISH] (High) `templates/admin/reports/revenue.html:51` + `churn.html:78` — Trend charts render `|default` zero series; routes (`reports.py:169,242`) never pass data → compute real series or remove card [recommend]
- [POLISH] (High) `app/services/web_reports.py:307,418,304,507-512` — fabricated KPIs (growth 12.5/8.3, recurring=total*0.85, churn_reasons via //) shown as real → compute or drop [recommend]
- [POLISH] (High) `web_reports.py:757,772` — `build_technician_export_csv` iterates empty `technician_stats` → header-only CSV → populate from `get_technician_report_data` [recommend]
- [POLISH] (High) `templates/admin/reports/mrr.html:43-44` — date_from/to inputs but backend only uses `year` → dead filter → Year selector or wire range [recommend]
- [POLISH] (Med) `web_reports.py:251,274,366,475` — `limit=1000` + Python aggregate understates totals/collection_rate/churn past 1000 → aggregate in SQL or "showing first N" [recommend]
- [POLISH] (Med) `templates/admin/dashboard/index.html:19` + `_stats.html:40` — "Real-time" but cached 60-180s, no "data as of" → last-updated stamp + soften label [recommend]
- [POLISH] (Med) `templates/admin/reports/bandwidth.html:38,132` — Download bound to `rx_bps` but `rx=upload` convention → swap or canonicalize via download_bps/upload_bps [recommend]
- [POLISH] (Med) `templates/admin/alerts/index.html:51-119` — accepts page/per_page + shows total but no pagination controls → add prev/next/per-page [recommend]
- [CONTROL] (Med) `app/services/admin_alerts.py:424,440,411` + `web_admin_dashboard.py:542` — alert thresholds hardcoded (reserved >100, queue >500, long-run >30m, splynx <7200s) → DomainSetting w/ ranges, single-sourced [recommend]
- [CONTROL] (Med) `web_admin_dashboard.py:209,702` — ONT low-signal `-25 dBm` + "offline >5" hardcoded + duplicated on ONT pages → single `ont_low_signal_dbm` setting (default -25) [recommend]
- [CONTROL] (Med) `web_reports_extended.py:147,233,283,213` + `web_reports.py:326,431,524` — row caps (200/100/20) + CSV cap (5000) hardcoded → export cap as setting/option + "truncated to N" footer [defer]
- [CONTROL] (Low) `app/web/admin/reports.py:365,539` + `revenue.html:25-29` — default window `days=30`, dropdown 30/90/365, network `hours` no UI → window as option + expose network window + custom range [defer]
