# UI Edge-Case Test Catalog

Tracked checklist of edge cases to exercise through the UI (admin, customer, reseller
portals) on a **disposable local stack** — never prod (see
`playwright-e2e-local-stack` notes). Tick items with date + PR as they're verified.

Legend: `[x]` verified OK · `[!]` bug found (see ref) · `[ ]` not yet tested.

Bugs found so far are tracked in **PR #224** unless noted. `#N` refers to the
running finding numbers from the 2026-06-12 driving session.

---

## 1. Auth, session & MFA
- [ ] Login wrong password ×N → account lockout + message + auto-unlock window
- [x] MFA wrong code rejected (2026-06-12)
- [ ] Wrong TOTP ×N → MFA-method lockout (migration 138)
- [ ] Expired TOTP step / ±window tolerance
- [ ] Concurrent sessions; "remember me 30d" persistence; absolute 30d cap
- [ ] Session expiry mid-form-submit → graceful redirect, no silent data loss
- [ ] Forgot-password: token reuse (single-use), expired, used-after-password-change, MFA not bypassed
- [ ] Invite: expired (24h), reused link, invite for already-activated account
- [ ] Logout invalidates back-button access; admin force-logout immediate
- [x] Cross-portal cookie isolation (customer cannot act on /admin or /reseller) (2026-06-12, partial)
- [ ] Email vs PPPoE password separation (customer reset never touches RADIUS)

## 2. Customer CRUD & onboarding
- [x] Duplicate email rejected, no duplicate row (2026-06-12)
- [ ] Duplicate phone; email case-sensitivity collision
- [!] `#18` Blank / whitespace-only / over-length required name fields were ACCEPTED on customer create+edit (server declared `first_name`/`last_name` as `Form(None)` and passed them raw; only `email` was validated) → customers created with empty display names. FIXED 2026-06-13: `_require_text()` trims, rejects blank/whitespace-only, and caps length (names 80, business name 120) across person/business create+edit; clean `ValueError` surfaced as a 400 form error. Verified live (whitespace → "First name is required", valid still creates) + unit tests. Emoji names still accepted (valid unicode, intentionally allowed)
- [x] Invalid email format rejected on customer create (`SubscriberCreate.email` is `EmailStr`) → 400, no row created (2026-06-13). Message is the verbose Pydantic dump (the known #12 "shared admin-form error formatter" residual, not re-fixed here). Phone format / international / long address still untested
- [ ] Wizard back/forward preserves data; cancel mid-wizard; double-submit → 1 customer
- [ ] Business vs individual (Contacts step business-only)
- [ ] Geocode unresolvable address; Nominatim unreachable
- [!] `#19` Edit customer email into a collision → **500 error**. Two bugs: (a) the person/business edit paths never pre-checked email uniqueness (unlike create), relying on the DB unique constraint; (b) worse, the edit handlers' `except` blocks re-queried the DB to re-render the form WITHOUT `db.rollback()`, so the aborted transaction turned the unique-violation into a 500 instead of a graceful 400. FIXED 2026-06-13: `update_person_customer` pre-checks email uniqueness (excluding self) → clean "A customer with email X already exists."; both edit handlers now `db.rollback()` before re-querying (matching the create handler). Verified live (collision now a 400 with the clear message, no 500) + unit tests (collision rejected, keeping own email allowed)

## 3. Catalog & offers
- [ ] Offer with 0 / negative / huge / non-numeric price
- [!] Offer with no plan_family (default) breaks instant change-plan — `#7` fixed PR #224
- [ ] Archived/hidden offer not customer-visible; "available for new services" off
- [ ] IP-Address plan kind without IP block size (validation)
- [ ] Offer with bandwidth unset → placeholder speed display
- [ ] Reseller/zone-scoped offer visibility

## 4. Subscriptions, lifecycle & enforcement
- [x] Suspend ↔ reactivate (status + RADIUS is_active flip) (2026-06-12)
- [x] Duplicate active subscription blocked (enforce_single_active held; 2nd active not created) (2026-06-12); minor: redirect to list without an obvious error flash
- [ ] Pending → service order created; Active → skips
- [ ] Static-IP plan with no IP pool → clear error
- [ ] Expire (past end_at) cuts access; vacation-hold auto-resume
- [ ] Cancel mid-cycle; reactivate cancelled/expired
- [ ] PPPoE reveal (staff-only); rotate service password → RADIUS resync
- [x] `#10` Plan-change now refreshes `unit_price` to the new offer's recurring price — fixed PR #224 (Subscriptions.update), unit-tested (2026-06-12)

## 5. Change-plan & proration
- [x] Upgrade quote + shortfall enforcement (2026-06-12)
- [x] Downgrade applies with family set (2026-06-12)
- [!] `#5` heading "Upgrade Summary" for downgrades — fixed PR #224
- [!] `#6` downgrade "₦0 remaining value" misleading — fixed PR #224
- [!] `#7` empty-family instant change 400 — fixed PR #224
- [ ] Change to the same plan (excluded)
- [ ] Cross-family change → migration/support ticket path
- [ ] Boundary: shortfall exactly 0; 1 kobo short
- [ ] Concurrent wallet debit / double-apply rapid clicks
- [ ] Prepaid → postpaid mismatch rejection
- [ ] Proration on billing day, day-before, far-from-renewal

## 6. Billing, invoices & payments
- [x] Offline payment settles invoice (2026-06-12)
- [!] `#9` paid-invoice payment form showed full total (overpayment risk) — fixed PR #224
- [x] `#11` Overpayment now caps allocation at the invoice balance and credits the surplus to the account wallet — fixed PR #224 (decision: cap + credit wallet), unit-tested (2026-06-12)
- [x] Partial payment → status `partially_paid` + correct remaining balance (2026-06-12)
- [x] Zero/negative amount rejected (Pydantic gt=0) (2026-06-12)
- [x] `#12` Payment form now formats validation errors cleanly (no raw Pydantic dump / pydantic.dev URL) — fixed PR #224 (payment_create handler). Broader admin forms still use str(exc); a shared formatter remains a wider follow-up
- [ ] Non-numeric amount; wrong currency vs invoice currency
- [ ] Customer-portal pay of an already-paid invoice (blocked/hidden)
- [!] `#23` Single invoice-**void** did not guard against voiding a **paid** invoice — `Invoices.void()` reversed the AR debits and set balance_due=0 while leaving the customer's payment allocated to a now-voided invoice (stranded money / AR desync). Inconsistent with **bulk**-void, which already skips `paid`/`void`. FIXED 2026-06-13: the canonical `Invoices.void()` now rejects `paid` ("refund the payment first") and already-`void` invoices with a 400 (protects all callers; bulk pre-check still stands). The admin UI already hides the void control on paid invoices, so this closes the server-side gap behind it. Unit-tested (paid → 400 + status unchanged; double-void → 400); existing draft-void test still passes. Refund path is already guarded (only `succeeded` payments refundable → double-refund 400)
- [ ] VAT inclusive/exclusive/exempt tax math; multi-line totals
- [x] Invoice PDF generates (2026-06-12)
- [ ] PDF for multi-line / VAT / zero invoice; brand name correct
- [ ] Dunning / arrangement screens with 0 and with many overdue
- [ ] Autopay with expired/failed card (failure-count cap)

## 7. Usage, FUP & quota
- [x] Usage page renders at 0 GB (2026-06-12)
- [ ] Near/over FUP threshold (approaching banner; throttle/block/suspend)
- [ ] fup_action throttle (no profile → no-op) / block / suspend
- [ ] Captive-redirect opt-in vs default for blocked customer (PR #216 flag)
- [ ] Quota rollover at period boundary; top-up GB before overage; period toggles
- [ ] Bandwidth chart with no / stale samples

## 8. Wallet & top-up
- [ ] Top-up happy path (NO real gateway) + `dotmacpay` return deep-link
- [x] Top-up amount limits enforced on web (below ₦1k min / above ₦500k max → button disabled + clear message; server also validates) (2026-06-12)
- [x] Bank-transfer proof flow is UNCAPPED (only amount>0) — correct; the ₦500k max is card/gateway-only (2026-06-12)
- [!] `#13` No bank-transfer option on the WEB Add Funds page (proof flow is mobile/API-only) → the ₦500k card cap has no transfer escape hatch on web; web customers can't self-serve a >₦500k top-up. Product decision: surface bank-transfer on web Add Funds
- [!] `#22` (NOTED, not fixed — VAS disabled in e2e + actively developed in parallel) `POST /portal/wallet/pay-bill` only validates that `amount` parses as a Decimal — it does NOT reject `<= 0`. A zero/negative amount reaches `vas_wallet.pay_bill` → `debit_wallet` (which only guards `amount > balance`, so a negative passes) → `Payments.create` rejects it via Pydantic `gt=0` → `ValidationError`. The route catches only `HTTPException`, so this surfaces as a **500** (not a clean error). No money is gained (the failure path reverses the debit symmetrically), so it's a UX/robustness gap, not a value bug. Today it's masked because `vas.enabled=false` → `require_enabled` returns 404 first. Recommended fix for the VAS owner: reject non-positive `value` at the route with a redirect-error (mirroring the "Invalid amount" branch), and/or guard `amount > 0` in `debit_wallet`
- [ ] Stranded intent reconcile; double-credit idempotency on gateway ref
- [x] Insufficient wallet blocks upgrade (via shortfall) (2026-06-12)
- Note: 5 console errors on Add Funds are external Paystack SDK assets blocked in sandbox (environmental, not a bug); Paystack SDK loads eagerly on page view

## 9. Support tickets & CRM
- [x] Create ticket from portal (2026-06-12)
- [!] `#20` Empty title was rejected (`min_length=1`) but **whitespace-only** title/comment-body slipped through (length ≥ 1, no strip) → blank-titled tickets. FIXED 2026-06-13: added `str_strip_whitespace=True` to the ticket/comment input schemas (TicketBase, TicketUpdate, TicketCommentBase/Update, MySupport*), so whitespace strips before `min_length` and good input is trimmed. Verified at the schema layer + unit tests
- [!] `#21` Admin new-ticket POST had **no** validation-error handling — an invalid title raised straight to a **500** (whitespace after #20; empty title already did). FIXED 2026-06-13: `ticket_create` now catches `ValidationError`/`ValueError`, rolls back, and re-renders the form at 400 with a clean "A ticket title is required." + preserved input; added an error banner to the template. Verified live (whitespace → 400 w/ message; valid still creates, title trimmed). NOTE: the customer-portal create path already returns 400 but via a generic "try again later" message (handle_ticket_create swallows the ValidationError) — a clearer portal message is a small follow-up
- [ ] Comment thread; close/reopen
- [x] Ticket-comment IDOR is SECURE (2026-06-13): the portal comment route derives `subscriber_ids` from the authenticated session (not client input) and `handle_ticket_comment` rejects any ticket whose `subscriber_id` is outside the caller's allowed set ("Ticket not found")
- [ ] CRM push unset (no-op) vs set; CRM unreachable → ticket still creates (async)
- [ ] Admin ticket vs CRM-native resolution (crm_subscriber_id)

## 10. Security — IDOR / RBAC / injection
- [x] IDOR invoice/ticket/PDF/pay 404 cross-customer (2026-06-12)
- [ ] `/me/*` JSON APIs, change-requests, arrangements, usage by id (session-scoped; lower risk)
- [x] Customer → /admin/* bounced to staff login; customer → /reseller/* → 303 no access (2026-06-12)
- [ ] Reseller view-as a customer outside their reseller (cross-reseller IDOR) — needs a 2nd reseller + foreign customer
- [ ] Object-scoped grants / wildcard perms (P1b) staff boundaries (covered by build-failing RBAC arch test)
- [x] XSS escaped — stored (customer name → detail) and reflected (search param); neither fired (2026-06-12)
- [ ] CSRF on state-changing POSTs; direct-POST gate bypass (change-plan archived-offer gate verified earlier)
- [ ] Rate limits on login / speedtest / forgot-password

## 11. Reseller portal
- [x] Login, dashboard, scoped accounts list (2026-06-12)
- [x] Revenue page renders at /reseller/reports/revenue (2026-06-12)
- [!] `#15` (self-inflicted, FIXED pre-merge) routing /reseller errors to reseller/errors/404.html 500'd because it extended layouts/reseller.html (needs current_user, absent in error context); rewrote it to extend base.html (standalone) + added regression tests. Caught by driving the reseller portal
- [!] `#16` (low pri) layouts/reseller.html doesn't guard `current_user` like layouts/customer.html does — latent fragility if ever rendered without auth context
- [x] `#17` (SECURITY) reseller web "View as customer" is now read-only — FIXED PR #224 (decision: reseller read-only, admin keeps write). Session carries `read_only=True`; a portal write-guard middleware blocks POST/PUT/PATCH/DELETE on /portal/* (except /portal/auth/ so Exit View works); banner shows "view-only"; prominent write CTAs hidden (started with support New Ticket). Verified live: ticket POST blocked (403, not created), Exit View works. Test added. Note: write-form GET pages still load (guard blocks the POST); the server guard backstops all writes. CTA-hiding sweep COMPLETED (follow-up branch): a global layout script hides every POST-form submit button (except Exit View via `data-allow-readonly`) and the Change Plan / Pause / Resume write links on service detail. Verified live: change-plan confirm, profile Save, and service write links hidden; POST /portal/profile still 403; Exit View works
- [ ] Reseller with 0 accounts (empty states); profile + MFA; bank-transfer proof
- [ ] Service-requests; only-own-reseller data everywhere

## 12. Notifications (keep sending OFF)
- [ ] Branded email wrapper + plain-text fallback; template vars populated
- [ ] Stale-queue 72h guard cancels; invoice.created burst
- [ ] Push (FCM) config-gated; inert without Firebase config

## 13. Mobile app
- [ ] Release build with prod --dart-define (not emulator 10.0.2.2)
- [ ] Token refresh after idle days; forced-logout on server session revoke
- [ ] Payment deep-link return; FUP card/banner; quota parity with web

## 14. Validation, formatting & i18n
- [x] Footer year dynamic (2026-06-12, PR #224)
- [ ] Currency formatting (₦ grouping, 2dp), negative amounts, everywhere
- [ ] Date/time zone display (CEST vs UTC); relative vs absolute
- [ ] Long names/plan names overflow; 0 and very large quantities
- [ ] Copyright/brand consistency across portals

## 15. Concurrency & idempotency
- [ ] Double-submit money forms (payment, change-plan, top-up) → no duplicate
- [ ] Two admins editing same customer/subscription
- [ ] Wallet read-modify-write race under concurrent debits
- [ ] Idempotent task re-runs (invoice cycle, autopay) don't double-charge

## 16. Empty / loading / error states & a11y
- [x] Several list empty states (customers/offers/invoices/tickets) (2026-06-12)
- [x] Unknown URL → branded 404 (no stack trace), shows Reference ID (2026-06-12)
- [x] `#14` Error handler now routes /portal & /reseller errors to their branded templates (correct dashboard link), not just /admin — fixed PR #224 (_template_response). Note: customer/reseller lack some status-code templates (403/409/500) and still fall back to the generic admin-linked one — a minor residual
- [x] Backend dep down (VictoriaMetrics) → graceful (logged errors, page renders) (2026-06-12, observed)
- [!] Console errors on change-plan page — `#4` fixed PR #224
- [ ] Keyboard nav / focus / aria on forms; dark-mode; responsive breakpoints
