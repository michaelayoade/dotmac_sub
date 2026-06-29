# Billing modules — UX-polish & operator-control audit

**Date:** 2026-06-29
**Method:** 6-agent parallel read-only review across the billing surface
(~16 admin pages, ~40 services): invoices/ledger/tax, payments/gateways/proofs,
dunning/collections/autopay, accounts/prepaid/reseller, customer pay portal,
settings/integrity/reconcilers.
**Status:** remediation in progress via draft PR #523. Companion to
[NETWORKING_UX_POLISH_AUDIT.md](NETWORKING_UX_POLISH_AUDIT.md).

> Note: recent money-state-machine PRs (#308 void/write-off/refund guards,
> row-locks, `written_off`; #204 webhook settlement) already hardened the engine
> guards. This audit is the UX / observability / configurability layer on top and
> excludes those guard additions.

## Remediation status

**Last updated:** 2026-06-29
**Tracking PR:** #523 (`audit/billing-remediation`)

### Resolved in current draft

- Reconciliation GET no longer writes/commits `BankReconciliationRun` rows by
  default; persistence is explicit via `persist_run`.
- Admin AR-aging now queries open receivables directly, excludes drafts and
  zero-balance invoices, and exposes currency-grouped totals.
- Billing account list stat cards now use computed totals/counts instead of
  fabricated zero values.
- Invoice, payment, ledger, AR-aging, payment import, reconciliation, and customer
  billing views now render currency codes/totals instead of assuming a naira glyph
  in the covered surfaces.
- Payment import audit metadata/history now preserves and displays grouped
  currency totals.
- Manual payment and credit forms now confirm before submit, disable their submit
  button after confirmation, and enforce a positive minimum amount in the UI.
- Dunning pause/resume/close, bulk pause/resume, payment-arrangement approval,
  payment-channel deactivation, and collection-account deactivation actions now
  prompt before changing money-collection state.
- Billing account search/status filters are now applied server-side with normal
  GET navigation, and the unsupported balance filter/dead HTMX table injection was
  removed.
- Dunning "View Details" now lands on a real guarded detail route with case
  status, account context, action controls, and action history.
- Invoice batch generation now redirects back to the full batch page with a
  user-visible note instead of returning a layout-less fragment.
- Billing automation and billing-health checks now import one canonical billable
  subscriber status set instead of carrying duplicated "keep in sync" constants
  and raw SQL literals.
- Consolidated "Record & distribute" now confirms before submit, disables the
  submit button after confirmation, enforces a positive minimum amount in the UI,
  and redirects back with success/safe-error feedback instead of exposing raw
  service exceptions.
- Fleet-wide Billing Settings save now confirms before submit, disables the
  submit button after confirmation, re-renders validation errors, and normalizes
  booleans, enum values, numeric ranges, decimal money fields, and CSV day lists
  before persistence.
- Bulk invoice mark-paid now returns processed and skipped counts with a
  partial-success message when selected invoices are missing, already paid, or
  not eligible.
- Bulk dunning pause/resume now redirects with processed and skipped counts so
  per-case failures are visible instead of looking like full success.
- Payment import results now report total, shown, and omitted error counts so the
  first 10-row error sample cannot hide additional failures.

### Partially resolved

- Currency display is improved across the touched admin/customer billing surfaces,
  but this audit's broader `default_currency`/provider/settings cleanup remains
  open for forms, adapters, Flutterwave, integrity SQL, and other untouched paths.
- AR-aging is fixed for the admin UI builder; the older reporting helper should be
  reviewed before marking the entire reporting-layer finding closed.

### Still open

- Remaining irreversible money-action server-side idempotency.
- Bulk/scheduled money-job observability, autopay panel, health/integrity admin UI,
  and raw exception copy cleanup.
- Customer post-payment-return states.
- Remaining billing settings/spec hygiene outside the covered Billing Settings
  form normalization.
- Remaining policy thresholds/settings work.

### Verification

- `poetry run pytest tests/test_billing_ar_aging_overview.py tests/test_billing_finance_workflows.py tests/test_billing_invoices_overview.py tests/test_billing_ledger_overview.py tests/test_billing_payment_import_options.py tests/test_billing_payments_overview.py`
  - Result: `45 passed`
- `poetry run ruff check tests/test_billing_money_action_templates.py`
  - Result: passed
- `poetry run pytest tests/test_billing_money_action_templates.py -q`
  - Result: `2 passed`
- `poetry run ruff check app/services/web_billing_accounts.py app/web/admin/billing_accounts.py tests/test_billing_accounts_list.py`
  - Result: passed
- `poetry run pytest tests/test_billing_accounts_list.py -q`
  - Result: `2 passed`
- `poetry run ruff check app/services/web_billing_dunning.py app/web/admin/billing_dunning.py tests/test_billing_dunning_detail.py`
  - Result: passed
- `poetry run pytest tests/test_billing_dunning_detail.py -q`
  - Result: `3 passed`
- `poetry run pytest tests/test_admin_route_permissions.py -q`
  - Result: `15 passed`
- `poetry run ruff check app/web/admin/billing_invoice_batch.py tests/test_billing_invoice_batch_web.py`
  - Result: passed
- `poetry run pytest tests/test_billing_invoice_batch_web.py -q`
  - Result: `8 passed`
- `poetry run ruff check app/services/billing_statuses.py app/services/billing_automation.py app/services/billing_health.py tests/test_billing_statuses.py`
  - Result: passed
- `poetry run pytest tests/test_billing_statuses.py tests/test_billing_health.py tests/test_billing_automation_services.py -q`
  - Result: passed
- `poetry run ruff check app/web/admin/billing_consolidated.py tests/test_billing_consolidated_web.py`
  - Result: passed
- `poetry run pytest tests/test_billing_consolidated_web.py -q`
  - Result: passed
- `poetry run ruff check app/services/web_system_config.py app/web/admin/system.py tests/test_billing_settings.py`
  - Result: passed
- `poetry run pytest tests/test_billing_settings.py -q`
  - Result: `10 passed`
- `poetry run ruff check app/web/admin/billing_invoice_bulk.py tests/test_billing_invoice_send_actions.py`
  - Result: passed
- `poetry run pytest tests/test_billing_invoice_send_actions.py -q`
  - Result: passed
- `poetry run ruff check app/web/admin/billing_dunning.py tests/test_billing_dunning_detail.py`
  - Result: passed
- `poetry run pytest tests/test_billing_dunning_detail.py -q`
  - Result: passed
- `poetry run ruff check app/services/web_billing_payments.py tests/test_billing_payment_import_options.py`
  - Result: passed
- `poetry run pytest tests/test_billing_payment_import_options.py -q`
  - Result: passed

## What this audit is

Two tracks (see the networking companion for the full definition):

- **POLISH** — make existing money features *feel finished and trustworthy*.
- **CONTROL** — expose hardcoded money policy as settings/options/safety-modes.

Billing surfaced a third concern worth separating: **correctness / data-integrity
bugs hiding behind "UX"** — these jump the queue ahead of cosmetic polish.

## Acceptance criteria (money-specific)

1. Every mutating money action: scope-named confirm + disable-on-submit/idempotency
   + visible result.
2. Bulk/batch report partial success; every scheduled money job surfaces last-run +
   counts + failures in-app.
3. No GET route writes; every button hits a real route; no dead filters or
   fabricated stats.
4. Money rendered in its own currency; dates tz-correct; status maps complete;
   financial reports exclude non-receivable states.
5. Customer pay flows have explicit pending/partial/decline states; never raw
   errors; never strand on blank email.
6. One canonical source for status-sets and policy thresholds (`settings_spec`);
   no duplicated "keep in sync" constants.

## Cross-cutting themes

### POLISH

**P-A. Irreversible money actions with no confirm / dup-submit guard.** Standard:
confirm naming action + scope ("affects N customers"), disable-on-submit +
idempotency on money-generating runs.
- Manual **Record Payment** (real money, defaults `succeeded`, auto-allocates) —
  no confirm/dup guard (`templates/admin/billing/payment_form.html:142`)
- Consolidated **Record & distribute** — no confirm, 500s on bad input (`app/web/admin/billing_consolidated.py:86`)
- **Issue Credit** — no confirm, no `min` (`templates/admin/billing/credit_form.html:34`)
- Dunning **Close / Pause-All / Resume-All** (`templates/admin/billing/dunning.html`)
- **Approve Arrangement** (`payment_arrangement_detail.html:66`)
- **Deactivate** payment channel / collection account (`payment_channels.html:123`, `collection_accounts.html:115`)
- **Run Batch** invoice run, no double-submit guard (`invoice_batch.html:110`)
- Fleet-wide **Save Billing Settings** applies enforcement policy, no preview/confirm (`templates/admin/system/config/billing.html`)

**P-B. Dead / broken UI** (these are bugs). Standard: every control hits a real
route; filters/stats wired or removed.
- Accounts **Deactivate/Delete** → routes don't exist (404/405, silent) (`templates/admin/billing/accounts.html:168`)
- Dunning **View Details** → no GET route, 404s every row (`dunning.html:200`)
- Accounts **filters** ignored by service; hx-target injects full page (`accounts.html:58`)
- Accounts **stat cards** always ₦0.00/0 (`accounts.html:29`)
- Invoice **Run Batch** lands on a layout-less bare fragment (`app/web/admin/billing_invoice_batch.py:28`)

**P-C. No partial-success / run observability on bulk & scheduled money jobs.**
Standard: bulk → "N of M, K failed"; every async/scheduled money job surfaces
last-run + counts + failures in-app; never raw exceptions.
- Bulk mark-paid no skipped count (`app/web/admin/billing_invoice_bulk.py:97`)
- Bulk dunning swallows per-case failures — 0-processed looks like success (`app/services/web_billing_dunning.py:119`)
- **Autopay has zero admin observability** (`app/services/autopay.py`)
- Reconcilers/remediation CLI-only, no last-run surfaced
- **Integrity/health only as Prometheus gauges**, no admin page (`app/services/billing_health.py:342`, `billing_integrity_audit.py:324`)
- Import errors truncated to 10 vs true count (`app/services/web_billing_payments.py:726`)
- Raw exception strings shown to operator (`app/services/web_billing_invoice_batch.py:56`)

**P-D. Money / currency / timezone display correctness.** Standard: render entity
currency; tz-correct dates; complete status maps; reports exclude non-receivables.
- Hardcoded `₦` despite multi-currency `Invoice.currency` (`invoice_detail/invoices/ledger/ar_aging`; `credits.html` does it right)
- Arrangement/extension amounts show no currency
- Naive-UTC timestamps with no tz (app tz Africa/Lagos)
- Status-badge map incomplete — `void/written_off/issued/partially_paid` → draft-grey (`invoice_detail.html:19`)

**P-E. Customer-facing post-payment-return states** (weight high — customer money).
Standard: explicit pending/settling/success/partial/decline states; never raw
errors; never strand.
- Verify-failure renders raw 400 + exception **even when the card may be charged** (`app/web/customer/routes.py:1701`)
- Gateway declines escape as 500 (only `ValueError` caught) (`routes.py:1638`)
- Blank `customer_email` strands Paystack (`templates/customer/billing/pay.html:185`)
- Underpayment success page hides remaining balance (`pay_success.html`)
- Silent saved-card capture failure, no confirmation

**P-F. Money-field validation.** Settings form persists money/CSV fields as raw
strings, no range/parse (`app/services/web_system_config.py:42`). Credit/consolidated
amounts have no `min`. Standard: validate+coerce server-side, re-render field errors.

### CONTROL

**C-1. Duplicated constants that drift (structural).** The **billable-account
status set** lives in 4+ places — incl. a raw SQL literal
`('active','blocked','suspended','delinquent')` — all carrying "keep in sync"
comments (`app/services/billing_health.py:314,54`; `billing_settings.py:14`;
`billing_integrity_audit.py:47`). → one canonical status module, imported everywhere.

**C-2. Policy thresholds/schedules hardcoded → settings** (defaults preserved):
- autopay `MAX_CONSECUTIVE_FAILURES=3` (retry cap *and* suspend threshold) (`app/services/autopay.py:64`)
- arrangement installment bounds `2–24` + default-on-`overdue>=2` (`app/services/payment_arrangements.py:147,546`)
- `MAX_EXTENSION_DAYS=30` (`app/services/service_extensions.py:31`)
- AR-aging buckets `30/60/90` (`app/services/billing/reporting.py:300`)
- billing-health alert thresholds (`SCAN_MIN_RATIO 0.5` etc.; "tune via ops" but needs deploy) (`billing_health.py:62`)
- reconcile sweep windows `15min/7d` not configurable — abandoned-redirect >1wk never recovered (`app/services/payment_reconciliation.py:110`)
- gateway HTTP `timeout=30` across Paystack/Flutterwave

**C-3. Settings-system hygiene.** Policy defaults live in the context builder, not
`settings_spec`: `suspension_grace_hours`, `dunning_escalation_days`,
`invoice_reminder_days` (`app/services/web_system_config.py:251`);
`blocking_period_days`/`deactivation_period_days`/`minimum_balance` have **no spec
entry at all** → displayed default can diverge from consumer default.
`billing_enabled_expected` invariant unregistered/invisible. → register every
billing policy key once with authoritative default+range.

**C-4. Currency hardcoded `NGN` despite an existing `default_currency` setting** —
forms/adapters (`credit_form`, `collection_accounts`, `billing_consolidated`,
`billing_adapter`), Flutterwave init (blocks non-NGN), integrity SQL, customer
portal. Single-currency today → mostly defer, but seed from the setting.

**C-5. Customer-facing controls to offer:** top-up presets `[1000…50000]` hardcoded
→ per-market setting (`app/services/customer_portal_flow_payments.py:1113`);
optional **partial-pay an invoice** (operator toggle); statement-period selector +
paperless/email-invoice opt-in.

## Priority

| Tier | Items |
|------|-------|
| **P0** | Reconciliation **GET route that writes+commits** dup rows (`app/services/web_billing_reconciliation.py:88`); **AR-aging counts drafts** → overstates receivables (`app/services/billing/reporting.py:271`); fix/remove **dead buttons + dead filters + fake stat cards + batch bare-fragment** (P-B); add **confirms on irreversible money actions** (P-A); **centralize billable-status set** (C-1) |
| **P1** | Customer **post-payment-return states** (P-E); **partial-success + run observability** incl. autopay admin panel + health/integrity admin page (P-C); **currency/tz/status display** (P-D); **settings validation + settings_spec hygiene** (P-F, C-3); **thresholds → settings** (C-2) |
| **P2** | currency-from-setting seeding (C-4), top-up presets + partial-pay + paperless (C-5), TTLs, gateway/provider timeouts |

## Cross-audit observation

The #1 structural risk is identical in networking and billing: load-bearing
constants duplicated across files with "keep in sync" comments — the networking
address-list name and the billing billable-status set. Worth a small shared
**"single-source-of-truth for status/policy constants"** initiative spanning both.

## Appendix — full findings by cluster

Format: `[POLISH|CONTROL] (severity) file:line — problem → recommendation [recommend|defer]`

### Invoices / ledger / credit-notes / tax / AR-aging
- [POLISH] (High) `templates/admin/billing/invoice_detail.html:167-171,268` (also `invoices.html:98,230,252`; `ledger.html:117-126`; `ar_aging.html:61,227`) — money hard-rendered `₦{{...}}` while `Invoice.currency` supports NGN/USD/EUR/GBP and `credits.html` honors currency → render entry currency [recommend]
- [POLISH] (High) `app/services/billing/reporting.py:271-276` — `get_ar_aging_buckets` includes `draft` in unpaid; pre-issue drafts counted as AR, overstating receivables → drop draft [recommend]
- [CONTROL] (Med) `app/services/billing/reporting.py:300-307` — aging thresholds 30/60/90 hardcoded → bucket-edges setting (default 30/60/90) [recommend]
- [POLISH] (Med) `app/web/admin/billing_invoice_bulk.py:97` — bulk mark-paid no skipped count though ineligible rows dropped (bulk void already reports) → report skipped consistently [recommend]
- [POLISH] (Med) `invoice_detail.html:19-29` — status badge styles only paid/pending/sent/overdue; issued/partially_paid/void/written_off fall through to draft-grey → extend map to all statuses [recommend]
- [POLISH] (Med) `invoice_form.html:78-84` + `app/services/billing/invoices.py:340-343` — currency select editable on edit but service rejects change with 400 → lock currency in edit mode [recommend]
- [CONTROL/POLISH] (Med) `app/services/web_billing_invoice_forms.py:78,135` — form hardcodes paymentTermsDays=30 while `resolve_payment_due_days` is the configurable source → pass resolved value into form config [recommend]
- [POLISH] (Med) `invoice_batch.html:110` — "Run Batch" (money-generating) no double-submit guard / disable (only Preview has spinner) → disable on submit / idempotency token [recommend]
- [POLISH] (Low) `app/services/web_billing_invoice_batch.py:56,222` — returns raw exception string into the page → log + generic message [recommend]
- [POLISH] (Low) `app/services/web_billing_invoice_batch.py:190` — batch preview truncates to [:50] silently → label "showing 50 of N" [defer]
- [CONTROL] (Low) `ar_aging.html:24-26` — period selector offers only All-time/This-year though `_period_bounds` supports month/quarter → expose richer set [defer]
- [POLISH] (Low) `app/services/billing/reporting.py:268,294` — aging compares `now(UTC).date()` vs `due_at.date()` tz-dropped; near-midnight off by a day → tz-aware compare (Africa/Lagos) [defer]
- Verified: `round_money` = ROUND_HALF_UP; invoice numbering/prefix/start, default currency/status/tax, batch schedule already settings-driven.

### Payments / gateways / webhooks / reconciliation / proofs
- [POLISH] (High) `templates/admin/billing/payment_form.html:142-145` — manual Record-Payment no confirm/disable; defaults `succeeded`, auto-allocates; no idempotency on POST → confirm + disable on submit [recommend]
- [POLISH] (Med) `app/services/web_billing_reconciliation.py:88-132` — `build_reconciliation_data` called from a GET route (`app/web/admin/billing_payments.py:674`) yet `db.add`+`commit`; refresh/filter persists duplicate `BankReconciliationRun` rows → move persist behind POST; render-only GET [recommend]
- [POLISH] (Med) `templates/admin/billing/payment_channels.html:123` (and `payment_channel_accounts.html:106`) — Deactivate posts with no confirm; stops payment routing → confirm [recommend]
- [POLISH] (Low) `app/services/web_billing_payments.py:726` — import result truncates `errors[:10]` while `total_errors` reports more → raise cap / "download full errors" [defer]
- [CONTROL] (Med) `app/services/paystack.py:110,166,199,259` & `flutterwave.py:107,143` — gateway HTTP `timeout=30` hardcoded everywhere → `payment_provider_http_timeout_seconds` setting (default 30, 5-120) [recommend]
- [CONTROL] (Med) `app/services/payment_reconciliation.py:110-111` — stale-topup sweep `older_than_minutes=15, max_age_days=7` as defaults, task passes no args; paid-but-abandoned >1wk never recovered → settings (default 15min/7d, range to 90d) [recommend]
- [CONTROL] (Low) `app/services/flutterwave.py:96` — `"currency":"NGN"` hardcoded in init (Paystack infers) → drive from invoice currency/default [defer]
- [CONTROL] (Low) `app/services/web_billing_payments.py:915-961` — admin manual payment no min/max guard (top-ups have settings) → optional max-manual-payment threshold [defer]
- Verified: proof verify/reject + refund have confirms+CSRF; reject requires reason; webhook dedupe via idempotency_key robust; bank-transfer instructions config-driven; import has loading/empty/partial-success.

### Dunning / collections / autopay / arrangements / extensions
- [POLISH] (High) `templates/admin/billing/dunning.html:200` — "View Details" links to a GET route that doesn't exist (only POST pause/resume/close); 404s every row → add case-detail GET+template or remove link [recommend]
- [POLISH] (High) `dunning.html:93,101,180,208` — Pause/Resume/Close + bulk Pause-All/Resume-All no confirm → add confirms (esp. Close + bulk) [recommend]
- [POLISH] (Med) `app/services/web_billing_dunning.py:119` + `app/web/admin/billing_dunning.py:116-143` — bulk action swallows per-case failures, routes discard processed list, no flash; 0-processed looks like success → surface "N of M / K failed" [recommend]
- [POLISH] (Med) `app/services/autopay.py` — no admin observability (suspended mandates, failure_count, run results); `get_status()` exists, no page consumes it → autopay admin panel [recommend]
- [POLISH] (Med) `payment_arrangement_detail.html:66` — Approve no confirm; activates dunning/suspension shield → confirm [recommend]
- [CONTROL] (High) `app/services/autopay.py:64` — `MAX_CONSECUTIVE_FAILURES=3` (retry cap + auto-suspend threshold) hardcoded → setting (default 3, range 1-10) [recommend]
- [CONTROL] (Med) `app/services/payment_arrangements.py:147-154` — installment bounds 2/24 hardcoded → settings (default 2/24, range 2-60) [recommend]
- [CONTROL] (Med) `app/services/service_extensions.py:31` — `MAX_EXTENSION_DAYS=30` hardcoded → setting (default 30, range 1-365) [defer]
- [CONTROL] (Med) `app/services/payment_arrangements.py:546` — defaults when `overdue_count>=2` hardcoded → missed-installment threshold setting (default 2, range 1-5) [defer]
- [POLISH] (Low) `payment_arrangements.html:120` + detail — amounts `"{:,.2f}"` no currency → prefix account currency [defer]
- [POLISH] (Low) `dunning.html:172,175` — timestamps strftime on UTC, no tz → render in tz or label UTC [defer]
- [CONTROL] (Low) `app/services/collections/_core.py:696` — suspension-notification idempotency 24h hardcoded → setting (default 24h) [defer]

### Accounts / deposits / prepaid / consolidated / reseller
- [POLISH] (High) `templates/admin/billing/accounts.html:168-182` — Deactivate (hx-post) + Delete (hx-delete) hit routes that don't exist (only GET); 404/405 silent → add routes (confirm + audit) or remove buttons [recommend]
- [POLISH] (High) `app/web/admin/billing_consolidated.py:86-106` — bulk "Record & distribute" no confirm + no try/except; `Decimal(amount)` raises on bad input → 500; no success feedback → confirm + try/except + flash [recommend]
- [POLISH] (High) `app/web/admin/billing_invoice_batch.py:28-43` — "Run Batch" response is bare unstyled `<div>` (no layout/nav) after highest-stakes action → redirect back to `/invoices/batch?note=...` [recommend]
- [POLISH] (Med) `accounts.html:58-99` — Search/Status/Balance filters submit via hx-get but `accounts_list` only accepts customer_ref/reseller_id; ignored + injects full page into table → wire params or remove [recommend]
- [POLISH] (Med) `accounts.html:29-53` — stat cards read total_balance/active_count/suspended_count never provided → always ₦0/0 → compute aggregates or drop cards [recommend]
- [POLISH] (Med) `credit_form.html:34,53` — Issue Credit no confirm, no `min` → confirm + `min="0.01"` [recommend]
- [POLISH] (Med) `collection_accounts.html:115` — Deactivate settlement account (payments reference it) no confirm → confirm [recommend]
- [CONTROL] (Med) `credit_form.html:39`, `collection_accounts.html:55`, `billing_consolidated.py:90`, `billing_adapter` — currency hardcoded NGN despite `default_currency` setting → seed from setting [recommend]
- [CONTROL] (Med) `app/services/web_billing_invoice_batch.py:410` / `invoice_batch.html:199` — run_day default 1, cap 1-28 hardcoded → consider end-of-month anchor option [defer]
- [CONTROL] (Low) `app/services/reseller_portal_billing.py:38` — `_INTENT_TTL=30min` hardcoded → setting (default 30m) [defer]
- [CONTROL] (Low) `app/services/vas_wallet.py:461` — dup-submit guard 60s hardcoded → setting if false positives [defer]
- Verified: VAS top-up min/max/daily limits + billing-run schedule already settings/flag-driven.

### Customer pay portal (web + mobile)
- [POLISH] (High) `app/web/customer/routes.py:1701-1708` — payment-return verify failure renders bare `errors/400.html` + raw exception even though card may be charged → dedicated "confirming your payment" state; reserve hard-error for genuine declines [recommend]
- [POLISH] (Med) `app/web/customer/routes.py:1638-1639` + `app/api/billing.py:1174` — pay routes only `except ValueError`; `charge_authorization` raises HTTPError on decline/5xx → 500 + generic JS → catch gateway errors, friendly decline copy [recommend]
- [POLISH] (Med) `templates/customer/billing/pay.html:185`, `topup.html:374` — `email` passed to Paystack with no guard; `_resolve_customer_email` can be "" → Paystack rejects, strands customer → if blank, disable Pay + prompt to add/verify email [recommend]
- [POLISH] (Med) `templates/customer/billing/pay_success.html:24-30,57-61` — underpayment still says "applied to your invoice", hides remaining balance → add "Remaining on invoice" + soften copy [recommend]
- [POLISH] (Med) `app/web/customer/routes.py:1675` + `app/api/me.py:698` — "Save this card" best-effort, swallows failures; success page doesn't confirm save → surface "Card saved" / "couldn't save" [recommend]
- [POLISH] (Low) `pay.html:138` — Pay button label from page-load amount while charge uses server intent; stale if balance changed → label off intent.amount [defer]
- [CONTROL] (Med) `app/services/customer_portal_flow_payments.py:1113` — top-up preset chips `[1000..50000]` hardcoded (min/max already settings) → DomainSetting presets per market [recommend]
- [CONTROL] (Med) `app/services/customer_portal_flow_payments.py:775-781` — invoice Pay only charges full balance_due; partial requires Add-Funds (auto oldest-first, can't target) → optional partial-pay field, operator toggle [defer]
- [CONTROL] (Low) `app/services/customer_portal_flow_billing.py:142` — billing index status-filter only; no statement-period selector / paperless toggle → add period selector + paperless pref [defer]
- [CONTROL] (Low) `customer_portal_flow_payments.py:730,846,889,1304` — currency hardcoded NGN, provider labels code constants → acceptable single-currency; revisit if multi-currency [defer]
- [CONTROL] (Low) `customer_portal_flow_payments.py:43,50` — `_TOPUP_INTENT_TTL` 30m / `_DIRECT_TRANSFER_TTL` 7d hardcoded → settings if ops want tuning [defer]
- Verified: per-session idempotency + `(provider_id, external_id)` unique + lock_account re-check; deliberate no-confirm-on-pay; CSRF-refresh retry; web/mobile parity.

### Billing settings / integrity / health / reconcilers
- [CONTROL] (High) `app/services/billing_health.py:314,327,335` — billable-status set re-hardcoded as raw SQL `('active','blocked','suspended','delinquent')`, a 3rd copy of the enums at `:54-59`, also `billing_settings.py:14-36` + `billing_integrity_audit.py:47-66`, all "keep in sync" → centralize one canonical status module; the SQL literal is highest risk [recommend]
- [CONTROL] (High) `app/services/billing_health.py:62-68` — alert thresholds module constants (`SCAN_MIN_RATIO=0.5`, `PAYMENT_VOLUME_MIN_RATIO=0.4`, `PAYMENT_BASELINE_MIN_DAILY=5.0`, `HEARTBEAT_STALE_MULTIPLIER=3.0`); "tune via ops" but needs deploy → settings_spec keys with ranges [recommend]
- [CONTROL] (High) `app/services/web_system_config.py:251-256` vs settings_spec — policy defaults in context builder not spec: `suspension_grace_hours="48"`, `dunning_escalation_days="3,7,14,30"`, `invoice_reminder_days="7,1"`; `blocking_period_days`/`deactivation_period_days`/`minimum_balance` have NO spec entry → register every key with one authoritative default+range; drop duplicate context defaults [recommend]
- [POLISH] (High) `app/services/web_system_config.py:42-72` (`_save_settings`) via `save_billing_config:263` — money/policy fields persisted as raw `.strip()` strings, no validation/coercion; no `min` on numeric fields; CSV unparsed; "-5"/"abc"/"3,7,foo" save silently → validate+coerce (numeric ranges, CSV-of-ints) + re-render field errors [recommend]
- [POLISH] (High) `templates/admin/system/config/billing.html:8-165` — single "Save" applies fleet-wide enforcement (auto_suspend, blocking/deactivation days, dunning schedule) with no preview/confirm → add confirm/diff gate "affects N customers" [recommend]
- [POLISH] (Med) `app/services/billing_integrity_audit.py:324` + `billing_health.py:342` — surfaced only as Prometheus gauges (only `app/tasks/billing.py:160`), no template → admin can't see launch_blocked/covered_but_locked/paid_with_balance or stale runners → billing health/integrity admin page with severity + empty/loading [recommend]
- [CONTROL] (Med) `app/services/crm_billing_push.py:54` — currency via `os.getenv("BILLING_DEFAULT_CURRENCY","NGN")`, bypassing settings_spec → resolve through settings_spec [defer]
- [CONTROL] (Med) `app/services/billing_health.py:256-280,307-338` — currency `'NGN'` hardcoded inside integrity SQL; single-currency assumption baked into correctness checks → derive from default currency [defer]
- [POLISH] (Med) `account_status_reconcile.py`, `stale_overdue_lock_reconcile.py`, `billing/unwall_paid_accounts.py`, `billing_remediation.py` — CLI-only, no last-run/result in admin → record + surface last-run/counts [defer]
- [CONTROL] (Med) `app/services/stale_overdue_lock_reconcile.py:65-72` — when no min-balance, silently defaults `Decimal("0.00")`; zero-balance treated as covered, could auto-restore → explicit/configurable fallback + log when defaulting [defer]
- [CONTROL] (Low) `app/services/billing_settings.py:75-101` (`check_billing_switch`) — `billing_enabled_expected` invariant read ad-hoc (DomainSetting→env→false), deliberately not a spec key, invisible in UI → register / surface next to `billing_enabled` [defer]
- Verified: `billing_remediation.py` (snapshot-drift refusal, never-delete, dry-run default, rollback manifest) + reconcilers' dry-run/eligibility gating are solid.
