# Edge-Case Test Matrix — DotMac Sub

Module-by-module edge-case results against the **`dotmac_test`** stack
(see [`TEST_ENV.md`](./TEST_ENV.md)), driven via Playwright at
`http://127.0.0.1:8010`.

**Last run:** 2026-06-14. **Drivers:** `scripts/testing/drive_edge_cases.py`
(authenticated full-nav crawl per role) + `scripts/testing/drive_targeted.py`
(interaction/negative/IDOR/RBAC/API). Raw results in `results/*.json`; **227
screenshots** in `results/screenshots/<role>/`.

**Status key:** ☐ not run · 🔄 in progress · ✅ pass · ❌ fail (see Findings) ·
⚠️ page renders OK but the *interaction* wasn't driven yet · ⏭️ blocked/skipped

## Coverage at a glance (full-nav crawl)
| Role | Login | Pages visited | Errors |
|------|-------|---------------|--------|
| admin | ✅ | 110 | 1 (404 dead link) |
| support | ✅ | (dashboard 403 — see F3) | RBAC-limited |
| finance | ✅ | (dashboard 403 — see F3) | RBAC-limited |
| active.customer | ✅ | 20 | 0 |
| overdue.customer | ✅ | 20 | 0 |
| prepaid.customer | ✅ | 19 | 0 |
| suspended.customer | ✅ | 19 | 0 |
| new.customer | ✅ | 17 | 0 |
| reseller | ✅ | 11 | 1 (500 — F2) |

**Net:** 226/228 distinct authenticated page-loads returned 200 with no crash.
The 2 exceptions + 3 other issues are in **Findings** below.

---

## 1. Customer Portal  (`/portal/...`)

| # | Edge case | Status | Notes |
|---|-----------|--------|-------|
| 1.1 | Login: valid customer | ✅ | active → `/portal/dashboard` |
| 1.2 | Login: **suspended** subscriber | ✅ | login allowed; all 19 portal pages render (incl. service detail) — no crash, no lockout |
| 1.3 | Login: wrong password ×6 | ✅ | lockout / rate-limit message shown |
| 1.4 | Login: unknown user | ✅ | generic error, **no user-enumeration leak** |
| 1.5 | Dashboard for **new** customer (no subscription) | ✅ | 17 pages render; empty-states clean, no 500 |
| 1.6 | Usage / FUP page (prepaid) | ✅ | `/portal/usage` 200 renders (web/mobile parity not deep-diffed) |
| 1.7 | Change-plan list excludes archived/inactive offers | ✅ | change page shows the active postpaid offer; **'Archived Legacy' & 'Inactive Draft' absent** |
| 1.8 | Change-plan apply an **archived/inactive** offer directly | ✅ | probe offer (status=inactive, is_active=True, portal-visible) POSTed by id → **400 rejected**, subscription unchanged. Memory's "instant-path gap" is **fixed** (guard now enforces status==active via get_available_portal_offers) |
| 1.9 | Top-up / buy bundle (prepaid) | ✅ | `POST /portal/billing/topup/intent` → 200 with paystack intent (`reference`, amount); empty public key → gateway needed to *complete* (graceful, no 500) |
| 1.10 | Pay invoice (overdue) | ⚠️ | pay page returns **400 + "payment unavailable"** (0 providers in test DB) — graceful, no crash; completion needs a configured gateway |
| 1.11 | Payment arrangement create (overdue) | ✅ | `POST /portal/billing/arrangements` → `?submitted=true`, `payment_arrangements` 0→1 |
| 1.12 | Profile: change password (email cred only) | ⚠️ | `/portal/profile` 200; change not driven |
| 1.13 | Tickets: create + view | ✅ | POST `/portal/support/new` → ticket created (count 0→1), visible on list |
| 1.14 | **IDOR**: access another customer's invoice/sub by id | ✅ | overdue→active invoice & sub both **404**; own resources 200 — cross-tenant blocked |
| 1.15 | Session: expiry / logout / absolute cap | ⚠️ | anon→login redirect ✅; full expiry not driven |

## 2. Billing & Payments  (admin `/admin/billing/...`, API `/api/v1/...`)

| # | Edge case | Status | Notes |
|---|-----------|--------|-------|
| 2.1 | Invoices overview loads | ✅ | `/admin/billing/invoices` 200 |
| 2.2 | Invoice detail: paid | ⚠️ | overview + ledger render; specific detail not asserted |
| 2.3 | Invoice detail: overdue | ⚠️ | as above |
| 2.4 | Record a payment (manual, API) | ✅ | `POST /api/v1/payments` → 201 |
| 2.5 | **Duplicate-payment guard (<1 min)** | ✅ | 1st=201, 2nd identical=**409** rejected |
| 2.6 | Payment with **no** account scope | ✅ | **F1 fixed** — now returns **422** |
| 2.7 | Dunning case list | ⚠️ | `/admin/billing/dunning` 200 |
| 2.8 | Payment arrangement back-office | ⚠️ | page loads |
| 2.9 | Webhook: payment settlement | ☐ | not driven (see §6.6/6.7) |
| 2.10 | Autopay run path | ☐ | not driven (no Celery in test stack) |
| 2.11 | Credit note create/apply | ⚠️ | `/admin/billing/credits` 200 |
| 2.12 | Money bounds: negative amount | ✅ | **F1 fixed** — now returns **422** |
| 2.13 | VAT/tax default on new invoice | ☐ | not driven |
| 2.14 | finance_manager can't reach non-billing admin pages | ✅ | system/network → 403; billing/customers → 200 |

## 3. Reseller Portal  (`/reseller/...`)

| # | Edge case | Status | Notes |
|---|-----------|--------|-------|
| 3.1 | Login w/ `reseller_users` link | ✅ | dashboard 200 (no redirect loop) |
| 3.2 | Accounts list scoped to reseller | ✅ | `/reseller/accounts` 200; its 2 sub-accounts' detail 200 |
| 3.3 | Cross-reseller IDOR (open a House customer) | ✅ | own account 200; another reseller's House customer → **404** |
| 3.4 | Revenue / commission view | ✅ | `/reseller/reports/revenue` 200 |
| 3.5 | View-as customer (read-only, audited) | ⚠️ | `/reseller/accounts/{id}/view` reachable; audit not asserted |
| 3.6 | Service request create/list | ✅ | **F2 fixed** — `/reseller/service-requests` now **200** |
| 3.7 | Bank-transfer proof upload | ☐ | route not surfaced in nav |
| 3.8 | Profile + MFA enable | ⚠️ | `/reseller/profile`, `/reseller/profile/mfa/setup` 200 |
| 3.9 | Reseller pay on behalf of customer | ⚠️ | `/reseller/billing` 200; flow not driven |
| 3.10 | VAS page | ❌ | **F5** — `/reseller/vas` **404** (nav link present) |

## 4. Admin & Network  (`/admin/...`)

| # | Edge case | Status | Notes |
|---|-----------|--------|-------|
| 4.1 | Admin dashboard loads | ✅ | admin 200 |
| 4.2 | Customers list + search | ✅ | `/admin/customers` 200 (search not asserted) |
| 4.3 | Customer detail: every state | ⚠️ | list loads; per-customer detail not individually opened |
| 4.4 | Impersonate customer | ✅ | admin POST impersonate → `/portal/dashboard` with `customer_session` set |
| 4.5 | **RBAC**: support can't reach admin-only pages | ✅ | system/roles, system/settings, network/core-devices → **403** |
| 4.6 | RBAC: customer can't reach `/admin/*` | ✅ | anon/portal → redirect; staff-gate holds |
| 4.7 | Network monitoring dashboard | ✅ | `/admin/network/monitoring` 200 (no live Zabbix; no hang) |
| 4.8 | OLT/ONT inventory pages | ✅ | `/admin/network/onts` etc. render empty cleanly |
| 4.9 | RADIUS pages | ✅ | `/admin/network/sessions` 200, no live-RADIUS crash |
| 4.10 | NAS list / create form | ⚠️ | page loads; validation not driven |
| 4.11 | Staff gate: unauthenticated `/admin/*` | ✅ | redirect to `/auth/login?next=...` |
| 4.12 | Dead nav link | ✅ | **F4 fixed** — link now → `/admin/network/alarms` (200) |

## 5. Settings & System  (`/admin/system/...`, `/admin/settings/...`)

| # | Edge case | Status | Notes |
|---|-----------|--------|-------|
| 5.1 | Settings hub loads | ✅ | `/admin/settings`, `/admin/system/settings` 200 |
| 5.2 | Update a setting + persistence | ☐ | not driven |
| 5.3 | Branding / white-label settings | ⚠️ | page loads |
| 5.4 | Email/SMTP settings + test send | ☐ | not driven (avoid sending) |
| 5.5 | Roles & permissions UI (RBAC builder) | ✅ | `/admin/system/roles` 200 (admin) |
| 5.6 | API keys: create / revoke | ⚠️ | page loads |
| 5.7 | Audit log view | ✅ | `/admin/system/audit` 200 |
| 5.8 | Secret fields masked / not leaked | ☐ | not asserted |
| 5.9 | Settings write as non-admin | ✅ | support → 403 on system/settings |

## 6. API & Webhooks  (`/api/v1/...`)

| # | Edge case | Status | Notes |
|---|-----------|--------|-------|
| 6.1 | `POST /api/v1/auth/login` happy path | ✅ | access+refresh tokens |
| 6.2 | Auth: bad creds / locked | ✅ | lockout after repeated failures |
| 6.3 | `/api/v1/me/*` self-scoping | ✅ | cross-customer resource → 404 (see 1.14) |
| 6.4 | Reseller self-scoped API | ☐ | not driven directly |
| 6.5 | Settings/scheduler permission gates | ✅ | staff least-privilege 403s confirmed |
| 6.6 | Payment webhook public endpoint auth | ✅ | `/api/v1/payment-events/{paystack,flutterwave}` reject unsigned body → **400** |
| 6.7 | Payment webhook idempotency (replay) | ☐ | not driven |
| 6.8 | Zabbix webhook auth + tags | ☐ | not driven |
| 6.9 | CRM webhook handling | ☐ | not driven |
| 6.10 | Invalid/oversized payloads → 422 not 500 | ✅ | **F1 fixed** — model-validator failures now 422 |
| 6.11 | Filter/query-param validation | ☐ | not driven |

---

## Findings log

| ID | Sev | Module | Summary | Root cause | Repro |
|----|-----|--------|---------|-----------|-------|
| **F1** | **High** | Billing API / global | ✅ **FIXED** — Requests that trip a Pydantic `@model_validator` or field constraint returned **500 instead of 422** | `app/errors.py` `validation_exception_handler` JSON-serialized the error `ctx`, which still held the raw `ValueError`. **Fix:** sanitize the whole error dict (`_sanitize_input(dict(error))`) so `ctx` is recursively coerced. Verified: no-scope & negative-amount payments now → **422**. | `POST /api/v1/payments {"amount":10,"currency":"NGN","status":"succeeded"}` (no account scope) → was 500, now **422** |
| **F2** | Med | Reseller | ✅ **FIXED** — `/reseller/service-requests` → **500** | `app/services/web_reseller_routes.reseller_service_requests_page` omitted `current_user` from the template context; `layouts/reseller.html` derefs `current_user.initials` (StrictUndefined). **Fix:** add `"current_user": context["current_user"]`. Verified: page now **200**. | login `reseller@test.local` → Service Requests |
| **F3** | Med | Admin / RBAC | ✅ **FIXED** — `support` & `finance` staff got **403 on `/admin/dashboard`** (their landing) | `app/web/admin/dashboard.py` required legacy broad `billing:read` (admin-only). **Fix:** `require_any_permission("billing:read","billing:invoice:read","customer:read")`. Verified: support & finance now → **200**. *(Reviewer note: this lets any staff with a basic read perm see the billing-KPI dashboard — tighten if billing figures must stay hidden from non-billing staff.)* | login `support@test.local` → `/admin/dashboard` |
| **F4** | Low | Admin / Network | ✅ **FIXED** — Dead nav link `/admin/network/monitoring/alarms` → **404** | Link in `templates/admin/network/monitoring/index.html` pointed at the wrong path. **Fix:** → `/admin/network/alarms`. Verified 200, dead link gone. | admin → Network monitoring → Alarms link |
| **F5** | Low | Reseller | ⏳ **documented (not fixed)** — `/reseller/vas` → **404** while the VAS nav link renders | `reseller_vas_page` raises 404 when `vas_wallet.is_enabled(db)` is False (off in test DB); the nav link in `layouts/reseller.html` always shows. **Recommended fix:** inject `vas_enabled` via the reseller branding context processor and gate the nav link — deferred (touches a perf-sensitive cached path for a cosmetic flag-off dead link). | login `reseller@test.local` → VAS |

### Suggested fixes
- **F1:** in `validation_exception_handler`, sanitize `ctx` before serializing — `str()` any non-JSON-native values (Pydantic v2 puts the exception object in `ctx['error']`). One fix covers all endpoints.
- **F2:** add `current_user` to the service-requests page context (mirror the other reseller page builders / the shared `require_reseller_context`).
- **F3:** change the dashboard guard to a permission support/finance actually have (e.g. `customer:read` or an `any_of`), or give them a role-appropriate landing page.
- **F4/F5:** fix or remove the dead nav links.

---

## What's left (next pass)
Covered: **page-render integrity (all roles), auth/lockout/enumeration, staff RBAC
403s, customer + reseller IDOR, API validation, webhook signature rejection,
duplicate-payment guard, change-plan offer exclusion + archived-offer apply guard,
customer ticket create, admin impersonation, payment-arrangement create, top-up
intent, invoice-pay graceful no-provider handling.**

Still to drive — each has an external side-effect, rotates a fixture, or needs a
secret: customer **change-password** (1.12, rotates the fixture login), reseller
view-as **audit assertion** (3.5), bank-transfer proof upload (3.7), settings
**persistence** + email **test-send** (5.2/5.4 — real ZeptoMail), secret masking
audit (5.8), webhook **idempotency** replay (6.7 — needs a valid provider
signature), zabbix/CRM webhook auth (6.8/6.9), and **completing** a top-up/pay
through a real gateway (1.9/1.10 — no provider configured in the test DB).
