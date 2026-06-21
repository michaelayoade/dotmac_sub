# FUP consumption windows — daily / weekly / monthly (#21)

## Problem
`FupRule.consumption_period` supports `daily` / `weekly` / `monthly` and the admin
UI lets you pick any of them, but enforcement ignores it: `evaluate_fup_rules`
reads `QuotaBucket.used_gb`, and `_period_bounds_for_record` hardcodes a UTC
**calendar month**. So a `daily` or `weekly` rule compares its threshold against
**monthly** usage — silently wrong. (Prod today: all 38 rules are monthly, so the
gap is latent — safe to build and verify before anyone configures sub-monthly.)

## Strategic rule
**One reader, one definition of a FUP window, multiple backing sources.** All of
enforcement, the customer usage summary, and notifications read usage through a
single function so they cannot drift apart.

## Approach
Build **A** (semantic source) first, then **B** (durable/materialized source)
behind the *same* reader.

### Approach A — semantic reader (ships first, makes daily/weekly work)
- **A1** `fup_window_bounds(period, now, tz)` → aligned `[start, end)` + `period_key` +
  tz. daily/weekly align to subscriber-local midnight/Monday; monthly keeps the
  UTC calendar month (matches QuotaBucket; existing rules unchanged).
- **A2** `get_fup_usage_gb(db, subscription, period, now) -> FupUsageWindow`
  (`used_gb`, window bounds, `source`, `is_authoritative`). monthly → QuotaBucket;
  daily/weekly → integrate `BandwidthSample`/VictoriaMetrics over the window
  (reuse `usage_summary` internals). Daily fits Postgres ~24h sample retention
  (sync); weekly needs VM (async bridge). When samples/VM are missing, return
  `is_authoritative=False` and do **not** hard-throttle on incomplete data.
- **A3** period-aware evaluation: load active rules once in sort order, compute
  usage once per *required* period, each rule compares against its period's
  window. Preserve chaining / sort / highest-severity selection.
- **A4** reset alignment: `cap_resets_at` = the rule's window `end` (daily→next
  local midnight, weekly→next Monday, monthly→QuotaBucket.period_end).
- **A5** customer usage-summary FUP block uses the *same* reader, so UI and
  enforcement agree.
- **A6** tests: window bounds; daily/weekly trigger on their window not monthly;
  monthly unchanged; mixed rules; reset alignment; summary alignment.

### Approach B — durable period buckets (follow-up, behind the same reader)
- **B1** new `fup_usage_buckets` table (separate from `QuotaBucket`, which carries
  billing concepts — allowance/rollover/topup/overage). Fields: subscription_id,
  period, period_key, window_start/end, used_gb, input/output bytes, source,
  is_complete; `unique(subscription_id, period, window_start, window_end)`.
- **B2** feed from the **delta stream** (RADIUS interim → bandwidth samples), not
  whole accounting sessions; split bytes across crossed boundaries; **idempotent**
  writes (delta identity / no double-count on retry) — without this B drifts up.
- **B3** reader prefers B when present+fresh, falls back to A; returns `source`.
- **B4** shadow mode: keep enforcing from A, compute B in parallel, log drift
  (per sub/period/threshold-proximity); backfill recent buckets.
- **B5** flip daily/weekly enforcement to B once drift is acceptable; keep A
  fallback; gate via `fup_submonthly_usage_source = samples | buckets |
  buckets_with_fallback`. monthly untouched throughout.

### Operational safeguards (before prod sub-monthly rules)
Admin warning when the sub-monthly source is unavailable; task-health metrics
(last bucket update, missing samples, VM failures, drift); log every sub-monthly
enforcement event with rule id, period, used/threshold GB, source, window, reset.

## Delivery order
A1 → A2 → A3 → A4 → A5 → A6 (PR 1), then B1 → B2 → B3 → B4 → B5 (follow-up PRs).
