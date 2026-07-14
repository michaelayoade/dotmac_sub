# Catalog & services modules — UX-polish & operator-control audit

> **Status: historical audit evidence.** Revalidate unresolved recommendations against `docs/UI_INFORMATION_AND_ACTION_STANDARD.md` and the current domain SOT before implementation.

**Date:** 2026-06-29
**Method:** 5-agent parallel read-only review across the catalog/services surface:
offers/plans/add-ons/pricing-calculator, subscriptions/change-plan/proration/
bulk-tariff, catalog-settings/FUP/usage, subscribers (= customers) admin,
service-requests + service-intent.
**Status:** audit only — nothing implemented from this doc yet. Companion to
[NETWORKING_UX_POLISH_AUDIT.md](NETWORKING_UX_POLISH_AUDIT.md) and
[BILLING_UX_POLISH_AUDIT.md](BILLING_UX_POLISH_AUDIT.md).

## What this audit is

Two tracks (full definition in the networking companion):

- **POLISH** — make existing features *feel finished and trustworthy*.
- **CONTROL** — expose hardcoded policy as settings/options/safety-modes.

This cluster has a distinctive third signature beyond the networking/billing
audits: **dead / misleading controls** — inputs shown to operators that do
*nothing* (a `start_date` that's ignored, an `ignore_balance` toggle with no
effect, a whole FUP settings page whose keys no code reads). These erode trust
worse than a missing feature because the operator believes they acted.

## Acceptance criteria (catalog/services-specific)

1. Every control shown does what it implies — no input is collected then ignored;
   no settings page writes keys nothing reads.
2. Any action with financial impact (change-plan, bulk-tariff, FUP rule) shows a
   **preview** (credit/charge/net, or affected-count) before commit.
3. Every mutating/lifecycle action has a confirm scaled to blast radius
   (single vs filtered-bulk vs all-customers), disable-on-submit, and a visible
   result; bulk reports partial success.
4. Numeric policy inputs are validated, not silently coerced to dangerous defaults
   (a 0-GB FUP threshold must be rejected, not throttle everyone).
5. Money rendered in its own currency; dates tz-correct; headers/stats reflect the
   real (server-side, unfiltered) data they claim.

## Cross-cutting themes

### POLISH

**P-A. Dead / misleading controls** (the signature issue — fix or remove).
- Bulk-tariff collects `start_date`, preview says "effective <date>", but `execute()`
  flips `offer_id` immediately (`templates/admin/catalog/bulk_tariff_change.html:173,210` + `app/services/bulk_tariff_change.py:72-151`)
- Bulk-tariff `ignore_balance` checkbox threaded through preview/execute but never
  read — no balance gate exists (`app/services/bulk_tariff_change.py:38,78`)
- FUP "Fair Usage Policy" settings page writes `FUP_KEYS` that **zero code reads**;
  real warn % lives in unrelated `usage_warning_thresholds` (`app/services/web_system_config.py:774-779`)
- Per-rule `speed_reduction_percent` persisted/shown to customers but never maps to
  bandwidth — two rules "50%" and "10%" throttle identically (`app/services/events/handlers/enforcement.py:473-518`)
- Service-request detail dropdown alphabetically default-selects the terminal
  **"Completed"** for a `new` request — one click jumps to non-reversible + emails reseller (`templates/admin/service_requests/detail.html:87-92`)
- Orphan bulk routes with no UI caller (`app/web/admin/customers.py:1927,2005`)

**P-B. No preview before applying a financial/lifecycle change.**
- Change-plan modal applies + generates proration invoice/credit with zero preview
  of credit/charge/net (`templates/admin/catalog/subscription_detail.html:103-135`)
- Bulk-tariff preview shows only plan *names*, no price/delta/total columns (`bulk_tariff_change.html:181-216`)
- FUP rule create/edit applies to ALL customers on the offer immediately, no
  preview/confirm (only "Delete this rule?" confirms) (`templates/admin/catalog/fup.html` / `_fup_sections.html:193`)

**P-C. Partial-success swallowed on bulk operations.** Standard: return
{changed, skipped, errored, failed_ids} and surface it.
- Subscriptions bulk activate/suspend/cancel/change-plan return success count only (`app/services/web_catalog_subscription_workflows.py:324-385`)
- Bulk-tariff "X changed, Y failed. Check the logs" — no failed list to triage (`bulk_tariff_change.py:99-151`)
- Customers bulk update ignores `updated_count`/`errors[]`, just reloads (`templates/admin/customers/index.html:1282`)

**P-D. Confirms missing / mis-scaled on high-blast actions.**
- Change-plan (financial) (`subscription_detail.html`), Update→terminal+notify
  reseller (`service_requests/detail.html:100-104`), FUP rule create/edit,
  Impersonate "Open Portal" (full read-write session as customer) (`templates/admin/customers/detail.html:581`)
- **Filtered bulk** (update/send) has no cap and no "apply to N customers?" — a
  filtered scope can hit the entire base with one click (`app/web/admin/customers.py:271`)

**P-E. Double-submit / no-visible-result.**
- Bulk change-plan button disabled only on `!targetOfferId`, not while pending →
  rapid clicks duplicate proration artifacts (`subscriptions.html:238-269`)
- `bulkAction()` catch only `console.error`s — failed bulk looks like nothing happened (`subscriptions.html:266-268`)
- Success toast dispatched then `location.reload()` destroys it (`subscriptions.html:256-259`)

**P-F. Display correctness.**
- Currency `₦` hardcoded despite `default_currency` setting — customer detail
  (`templates/admin/customers/detail.html:268,320,...`) and calculator totals
  (`templates/admin/catalog/calculator.html:153-177`)
- tz-naive `.strftime` timestamps (`customers/detail.html:637,...`)
- "Active Subscriptions" header but query passes `status=None` (lists blocked/
  suspended/canceled) (`offer_detail.html:316` vs `web_catalog_offers.py:702-711`)
- Service-request "Completed" stat counts current page only (`service_requests/index.html:52-57`)
- Calculator VAT computed on subtotal only; one-time fees shown VAT-free; "First
  Bill" ignores proration (`calculator.html:305-312`)

### CONTROL

**C-1. Dangerous silent coercion (correctness, customer-impacting).**
- FUP `threshold_amount` typo silently `float()`→`0.0`; a 0-GB threshold makes
  `usage >= 0` always true → throttles/blocks **every** customer on the offer
  (`app/services/web_fup.py:251-254`). Same silent-0 for `sort_order`. → reject
  invalid numbers.

**C-2. FUP control-surface split / drift (structural).**
- Admin FUP page keys are dead (C above); the real gate
  `fup_submonthly_rules_enabled` is read via `resolve_value` but **not registered**
  in settings_spec/seed, so ops must hand-insert a DB row to enable daily/weekly
  (`app/services/web_fup.py:219`)
- Default warn ratio `0.8` hardcoded in 2+ places (`app/services/usage.py:302`,
  `usage_summary.py:196`) plus the spec default `"0.8,0.9"` — drift risk
- Throttle depth is a single global `fup_throttle_radius_profile_id`; per-rule %
  decorative (see P-A) → derive profile from % or document advisory

**C-3. Catalog defaults that bypass existing settings.**
- New offers default `show_on_customer_portal=True` + `available_for_services=True`
  (the footgun behind the "~40 e2e offers wrongly customer-visible" incident) →
  new-offer visibility default OFF, as a catalog setting (`app/services/web_catalog_offers.py:179-180`)
- Offer form hardcodes `price_currency='NGN'`, overriding `billing.default_currency`
  honored by `OfferPrices.create` (`web_catalog_offers.py:197,258`)
- `vat_percent` per-offer free-text, blank default, no prefill from a configurable
  default VAT (`offer_form.html:604-607`)

**C-4. Lifecycle timing/scope hardcoded.**
- Change-plan effective timing is hardcoded **instant**; no "next cycle" option
  (`invoice_timing` only controls invoice generation) (`app/services/catalog/subscriptions.py:1498-1520`)
- Bulk-tariff / bulk change-plan hardcoded `status==active` only; suspended subs
  silently excluded and under-counted in preview (`bulk_tariff_change.py:58,93`)
- next_billing recompute staleness threshold hardcoded 60d (`subscriptions.py:1606`)

**C-5. Thresholds an operator would want to tune.**
- Serviceable radius `_SERVICEABLE_RADIUS_KM=1.5` hardcoded, drives serviceable/not
  (`app/services/reseller_service_requests.py:26`)
- No service-request SLA/aging threshold exists (pairs with an aging badge)
- Password-reset throttle "3/hr" hardcoded in 2 places while invite expiry IS a
  setting (`app/services/web_customer_user_access.py:319,492`)
- PPPoE-reveal rate limit `30/3600s` hardcoded (`app/web/admin/customers.py:748`)
- GiB computed but labelled "GB" — "100 GB" rule is really ~107 GB (`app/services/fup.py:356-360`)

## Priority

| Tier | Items |
|------|-------|
| **P0** | FUP `threshold_amount` silent-0 → throttles/blocks everyone (C-1); kill/fix **dead controls** that imply money/lifecycle effect — bulk-tariff `start_date`, `ignore_balance`, dead FUP settings page (P-A); confirms on change-plan + impersonate + service-request terminal transition + filtered-bulk scope (P-D); new-offer default visibility OFF (C-3, recurrence of a real incident) |
| **P1** | Preview-before-apply for change-plan / bulk-tariff / FUP rule (P-B); partial-success on all bulk catalog ops (P-C); FUP control-surface consolidation + register `fup_submonthly_rules_enabled` + centralize 0.8 (C-2); currency/tz/header display (P-F); change-plan effective-timing instant│next_cycle (C-4); double-submit guards (P-E) |
| **P2** | thresholds → settings (serviceable radius, SLA aging, reset throttle, pppoe reveal, 60d) (C-5); GiB labeling; usage-priced offers in UI; calculator VAT/proration accuracy |

## Cross-audit observation

The **drift-from-duplicated-constants** pattern recurs a third time (FUP `0.8` in
2+ places), and the **dead/misleading-control** pattern is this cluster's
signature. Combined with networking's address-list and billing's billable-status
set, the strongest systemic recommendation across all three audits is a shared
**single-source-of-truth for status/policy constants + a "no dead controls" lint**
(every form field maps to a consumer; every settings key has a reader).

## Security note (out of the two tracks, flagged for triage)

Most write routes in `app/web/admin/catalog_settings.py` (usage-allowance / SLA /
policy-set / add-on create/update/delete/bulk-delete) lack a route-level
`require_permission("catalog:write")` — only `region_zone_create` (line 145) has
it. Likely covered by mount-registry RBAC guards (per the RBAC overhaul), but
**verify these writes aren't reachable with only `catalog:read`.**

## Appendix — full findings by cluster

Format: `[POLISH|CONTROL] (severity) file:line — problem → recommendation [recommend|defer]`

### Offers / plans / add-ons / pricing-calculator
- [CONTROL] (High) `app/services/web_catalog_offers.py:197,258` + `offer_form.html:356` — form hardcodes `price_currency='NGN'`, overriding `billing.default_currency` honored by `OfferPrices.create` → seed from setting [recommend]
- [CONTROL] (Med) `web_catalog_offers.py:179-180` — new offers default `show_on_customer_portal=True` + `available_for_services=True` (the ~40-visible-offers footgun) → catalog setting, default OFF [recommend]
- [POLISH] (Med) `calculator.html:153,161,169,173,177` — overage/VAT/monthly/one-time/first-bill totals hardcode `"NGN "` while base/add-on rows use `price.currency` → use offer currency consistently [recommend]
- [POLISH] (Med) `calculator.html:305-312,176-177` — VAT on subtotal only; one-time added after VAT (shown VAT-free); first-bill ignores proration → apply VAT per rules + label estimate un-prorated [recommend]
- [POLISH] (Med) `calculator.html:240-246` — no linked add-ons → silently shows ALL add-ons ("backwards compat") → show empty-state instead [recommend]
- [CONTROL] (Med) `offer_form.html:604-607` — `vat_percent` per-offer free-text, blank default, no prefill from configurable default VAT → prefill (default e.g. 7.5%, 0-100) [recommend]
- [POLISH] (Med) `offer_detail.html:316` vs `web_catalog_offers.py:702-711` — "Active Subscriptions" header but query `status=None` lists blocked/suspended/canceled → filter active or rename "Recent" [recommend]
- [CONTROL] (Low) `web_catalog_offers.py:923` — `price_types` hardcoded `["recurring","one_time"]`, excludes `usage` though enum + validation support it → expose full enum / gate by setting [defer]
- [CONTROL] (Low) `calculator.html:368` — "seems low" check hardcodes `monthlyTotal > 100` (currency-naive) → catalog setting (default 100, per-currency) [defer]
- [CONTROL] (Low) `web_catalog_offers.py:69` — `IP_BLOCK_SIZES` /32../24 hardcoded → consider setting for operators restricting/extending block sizes [defer]
- [POLISH] (Low) `offer_detail.html:78-84,109-116` — "Bandwidth" + "Download/Upload" rows render identical values → drop duplicate row [defer]
- Verified: archive/restore have hx_confirm + reload; bulk-tariff surfaces changed/skipped/errors + rollback; data_grid empty-state/search/pagination; status→is_active reconcile.

### Subscriptions / change-plan / proration / bulk-tariff
- [POLISH] (High) `subscription_detail.html:103-135` + `runChangePlan() :172` — change-plan applies + generates proration with zero preview of credit/charge/net, no confirm → add proration preview before enabling Change Plan [recommend]
- [POLISH] (High) `bulk_tariff_change.html:173,210,237` + `bulk_tariff_change.py:72-151` — collects `start_date`, preview says "effective <date>", but `execute()` flips immediately → honor start_date (schedule) or remove + relabel "applies now" [recommend]
- [CONTROL] (High) `app/services/catalog/subscriptions.py:1498-1520,854-953` — change-plan timing hardcoded instant; no next-cycle (invoice_timing only controls invoicing) → per-action instant│next_cycle via SubscriptionChangeRequest.effective_date, default instant [recommend]
- [POLISH] (Med) `web_catalog_subscription_workflows.py:324-385` + `web_catalog_subscriptions.py:3850,3900` — bulk activate/suspend/cancel/change-plan return success count only; skipped/errored dropped → return {changed,skipped,errored,failed_ids} + surface [recommend]
- [POLISH] (Med) `bulk_tariff_change.html:366` + `bulk_tariff_change.py:99-151` — "X changed, Y failed. Check the logs" — no failed list → collect failed (id,error) + render [recommend]
- [POLISH] (Med) `subscriptions.html:238-269,289-322` — bulk modal no in-flight guard; rapid clicks duplicate POSTs (dup proration) → `busy` flag disables + short-circuits [recommend]
- [POLISH] (Med) `subscriptions.html:266-268` — `bulkAction()` catch only console.error; no toast → dispatch error toast [recommend]
- [POLISH] (Med) `bulk_tariff_change.html:181-216` — preview shows only plan names, no price/delta/total; also bypasses subscriptions.update (no prorate/re-snapshot — separate correctness look) → add price/delta/total columns [recommend]
- [POLISH] (Med) `bulk_tariff_change.py:38,78` + `web_bulk_tariff_change.py:36,99` — `ignore_balance` collected/threaded/shown but never read → implement the balance gate or remove checkbox [recommend]
- [CONTROL] (Med) `bulk_tariff_change.py:58,93` + `web_catalog_subscriptions.py:3907` — hardcoded `status==active` only; suspended silently excluded + under-counted → configurable included-statuses / surface "N suspended not included" [defer]
- [POLISH] (Low) `subscriptions.html:256-259,309-312` — success toast then immediate reload destroys it → flash/`?notice=` after reload [defer]
- [CONTROL] (Low) `subscriptions.py:1606` — next_billing recompute staleness hardcoded 60d → billing setting (default 60d) [defer]

### Catalog-settings / FUP / usage
- [CONTROL] (High) `app/services/web_system_config.py:774-779` — FUP config page (`FUP_KEYS`) is write-only/dead (zero readers); real warn % in `usage_warning_thresholds` → wire to enforcement or remove page [recommend]
- [CONTROL] (High) `app/services/events/handlers/enforcement.py:473-518` — throttle always applies single global profile; per-rule `speed_reduction_percent` never maps to bandwidth → derive profile from % or document advisory-only [recommend]
- [CONTROL] (High) `app/services/web_fup.py:251-254` — bad `threshold_amount` silently `float()`→0.0; 0-GB threshold throttles/blocks everyone; same for `sort_order` → reject invalid numbers [recommend]
- [CONTROL] (Med) `app/services/web_fup.py:219` — `fup_submonthly_rules_enabled` read via resolve_value but not registered in settings_spec/seed → add SettingSpec [recommend]
- [CONTROL] (Med) `app/services/usage.py:302` + `usage_summary.py:196` — default warn ratio 0.8 hardcoded in 2+ places (+ docstring) vs spec default "0.8,0.9" → centralize default [recommend]
- [CONTROL] (Med) `app/services/fup.py:591-592` + `web_fup.py:404-405` — `billing_cycle_days=30`/`billing_day_elapsed=15` hardcoded for projection, ignores real cycle → pass real cycle [defer]
- [CONTROL] (Med) `app/services/settings_spec.py:1985-1993` — `fup_action` (throttle/suspend/block/none) is a second action knob alongside each rule's `action` enum; precedence undocumented → document or collapse [defer]
- [CONTROL] (Low) `app/services/fup.py:356-360` + `usage_summary.py:57` — thresholds in GiB but labelled "GB" → relabel GiB or use 10⁹ [defer]
- [POLISH] (High) `templates/admin/catalog/fup.html` vs `_fup_sections.html:193` — rule add/edit applies to ALL customers immediately, no preview/confirm → confirm/diff on create/edit [recommend]
- [POLISH] (Med) `_fup_sections.html:247-248` — "Speed Reduction %" no help, ambiguous (to vs by) and ignored at enforcement → help clarifying advisory + real throttle = global profile [recommend]
- [POLISH] (Med) `usage_allowance_form.html:51-81` + `policy_set_form.html:62-95` — numeric fields have `min` but no `max`/server bound; no unit cross-check (GB vs GiB) → ranges + units help [defer]
- [POLISH] (Low) `usage_warning_thresholds` (ratio 0.8,0.9) vs FUP_KEYS `*_warn_pct` (percent) — mixed ratio/percent of same concept, no hint → standardize + label units [defer]

### Subscribers (= customers) admin
> Note: `app/web/admin/subscribers.py` is a 34-line back-compat alias; real routes in `app/web/admin/customers.py`, templates under `templates/admin/customers/`.
- [POLISH] (High) `templates/admin/customers/index.html:1282` — `applyBulkUpdate()` ignores server `updated_count`/`errors[]`, just reloads; per-row failures swallowed (cf. `queueBulkMessage :1347`) → show "Updated N, M failed" [recommend]
- [POLISH] (Med) `customers/detail.html:581` — Impersonate "Open Portal" no confirm before full read-write session as customer → add confirm [recommend]
- [POLISH] (Med) `app/web/admin/customers.py:271` (`resolve_bulk_customer_scope` → `web_customer_actions.py:312`) — filtered bulk has no cap / no count confirm; one click can hit entire base → "apply to N customers?" confirm [recommend]
- [POLISH] (Med) `customers/detail.html:268,320,835,1246-1258,1286,1326,1524` — `₦` hardcoded despite `default_currency` (`smart_defaults.py:73`) → render from setting [recommend]
- [POLISH] (Med) `customers/detail.html:637,682,1167,1220,1322` — naive `.strftime` timestamps, no tz label → localize / append tz [defer]
- [POLISH] (Low) `customers/detail.html:651` — "Send Reset Link" no confirm while deactivate-login/reset-mfa do → consistent friction [defer]
- [POLISH] (Low) `customers/detail.html:613` — PPPoE reveal no UI confirm (server audit + 30/hr exist, so low) → optional confirm [defer]
- [POLISH] (Low) `app/web/admin/customers.py:1927,2005` — `/bulk/status` + `/bulk/delete` have no UI caller → remove or wire button [defer]
- [CONTROL] (Med) `app/services/web_customer_user_access.py:319,492,496` — password-reset throttle "3/hr" hardcoded twice while invite expiry IS a setting → `auth.password_reset_max_per_hour` (default 3, 1-20) [recommend]
- [CONTROL] (Med) `app/web/admin/customers.py:748` — PPPoE-reveal limit `30/3600s` hardcoded → `security.pppoe_reveal_max_per_hour` (default 30) [defer]
- [CONTROL] (Low) `app/web/admin/customers.py:235` (+ `smart_defaults.py:108`) — convert/create defaults `account_status="active"` → `default_subscriber_status` setting (default active) [defer]
- [CONTROL] (Low) `app/web/admin/customers.py:269,598,678` — page-size cap `le=100` hardcoded across 3 routes → centralize configurable cap [defer]
- Verified: delete/deactivate/reset-mfa/deactivate-login have confirms; PPPoE reveal audited + rate-limited.

### Service-requests + service-intent
- [POLISH] (High) `service_requests/detail.html:87-92` — `allowed_next` sorts alphabetically so a `new` request default-selects terminal **"Completed"**; one Update click → non-reversible + emails reseller → order by workflow, don't default a terminal state [recommend]
- [POLISH] (High) `service_requests/detail.html:100-104` — "Update & Notify Reseller" no confirm; completed/rejected terminal + irreversible notification → confirm, stronger wording for terminal [recommend]
- [POLISH] (High) `reseller_service_requests.py:205-238` / `web_service_requests.py:30-44` — marking `completed` does nothing operational (no subscription/intent/provisioning fired despite "feeds activation"); generic "updated" message → add visible Convert/Provision action + surface result [recommend]
- [POLISH] (Med) `service_requests/index.html:52-57` — "Completed" stat counts only current page (`selectattr`), not total → compute server-side like `new_count` [recommend]
- [CONTROL] (Med) `reseller_service_requests.py:26` — `_SERVICEABLE_RADIUS_KM=1.5` hardcoded, drives serviceable/not → setting (default 1.5km, 0.5-10) [recommend]
- [POLISH] (Med) `reseller_service_requests.py:154,171` — `nearest_plant_km` computed at submit then discarded; detail shows only coarse badge, no distance/"as of"/recheck → persist + show + re-check action [recommend]
- [POLISH] (Med) `service_requests/index.html` + `web_service_requests.py:38` — no SLA/aging signal for requests sitting too long → aging badge/sort backed by configurable threshold [recommend]
- [CONTROL] (Med) (no SLA setting anywhere) — add request aging/SLA threshold (e.g. new>3d overdue, 1-30d) [defer]
- [POLISH] (Med) `service_requests_queue.py` + `index.html` — no bulk select / no assignee-claim; multiple admins can work same request; no partial-success path → row-claim + (if bulk) per-row reporting [defer]
- [POLISH] (Low) `service_requests/index.html:125-131` — empty state always "No service requests found" even when filtered → vary copy + "clear filter" when status set [recommend]
- [POLISH] (Low) `service_requests/index.html:65` — `onchange="this.form.submit()"` auto-submit, no fallback button (keyboard/SR/no-JS) → add Apply button [defer]
- [CONTROL] (Low) `service_intent_ui_adapter.py:118` — hardcoded `connection_type="pppoe"`, service_type "internet", planned name "Internet" → promote to offer/profile config if non-PPPoE deployments appear [defer]

## Remediation status

### Resolved (P0 pass, 2026-07-03)

- **C-1 FUP silent coercion** — `threshold_amount` / `speed_reduction_percent` /
  `sort_order` now validate and 400 on bad input (positive/finite threshold,
  1–99 speed %, integer sort order) in both add and update paths
  (`app/services/web_fup.py`); regression tests in `tests/test_fup_ui_gaps.py`.
- **P-A bulk-tariff dead controls** — `start_date` and `ignore_balance` removed
  from the form, preview, hidden inputs, and service signatures; preview/confirm
  copy now says the change applies immediately. `execute()` returns `failed_ids`
  and the result page links each failed subscription (partial P-C).
- **P-A dead FUP settings page** — already removed by the system-config
  remediation (section 8.25 tombstone in `web_system_config.py`).
- **C-3 new-offer visibility** — new-offer form defaults
  `show_on_customer_portal` / `available_for_services` to **off**; opt-in via
  new registered catalog setting `new_offer_visible_by_default` (default false).
- **P-D confirms** — change-plan modal confirms with plan name + proration
  warning; impersonate "Open Portal" confirms (read-write session, audited);
  service-request status select no longer preselects terminal "Completed"
  (placeholder + "(final)" markers) and submit confirms, strongest for terminal;
  customers filtered-bulk update **and** bulk message queue confirm with the
  actual scope count ("apply to N customers?").

### Resolved (P1 pass, 2026-07-03)

- **P-C partial success** — subscription bulk activate/suspend/cancel/change-plan
  return `{changed, skipped_ids, failed_ids}`; the UI surfaces skipped/failed
  IDs in a blocking summary instead of a bare success count.
- **P-E double-submit** — bulk change-plan and bulk actions guard on a busy
  flag (button disabled + "Applying…"); the success toast now survives the
  page reload (sessionStorage replay) instead of being destroyed by it.
- **P-B (partial)** — bulk-tariff preview shows source→target recurring price
  and the per-cycle delta (real currency code, not hardcoded ₦).
- **C-2 (partial)** — `fup_submonthly_rules_enabled` registered in
  settings_spec (usage domain, default off); FUP warn-ratio fallback 0.8
  centralized as `usage.DEFAULT_FUP_WARN_RATIO`.

### Resolved (P1 follow-up, 2026-07-03)

- **P-B change-plan proration preview** — the admin change-plan modal fetches
  a live quote (`GET /admin/catalog/subscriptions/{id}/change-plan-quote`,
  reusing the portal quote builder) and shows credit-for-unused-time, prorated
  new-plan charge, and net before the confirm.
- **Security note verified** — all 20 write routes in
  `app/web/admin/catalog_settings.py` carry route-level
  `require_permission("catalog:write")`; reads are covered by the
  mount-registry RBAC layer.

### Still open

- **P1 remainder**: FUP rule impact preview; currency/tz display sweep
  (customer detail, calculator); change-plan instant│next-cycle timing
  (product decision).
- **P2**: tunable thresholds (serviceable radius, SLA aging, reset throttle,
  PPPoE reveal limit, 60d staleness); GiB labeling; usage-priced offers in UI;
  calculator VAT/proration accuracy.
- **Security note**: verify `catalog_settings.py` write routes against the
  mount-registry RBAC layer (unchanged by this pass).
