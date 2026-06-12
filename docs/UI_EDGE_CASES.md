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
- [ ] Blank / whitespace-only / max-length-overflow / emoji-unicode required fields
- [ ] Invalid email & phone formats; international phone; long address; special chars
- [ ] Wizard back/forward preserves data; cancel mid-wizard; double-submit → 1 customer
- [ ] Business vs individual (Contacts step business-only)
- [ ] Geocode unresolvable address; Nominatim unreachable
- [ ] Edit customer email into a collision

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
- [!] Plan-change leaves `unit_price` stale — `#10` (follow-up, not yet fixed)

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
- [!] `#12` Validation errors render as raw Pydantic strings to admin (leaks internals + pydantic.dev URL) — minor UX, FOLLOW-UP (cross-cutting: needs a shared admin form-error formatter)
- [ ] Non-numeric amount; wrong currency vs invoice currency
- [ ] Customer-portal pay of an already-paid invoice (blocked/hidden)
- [ ] Void / refund invoice with a payment; refund > paid
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
- [ ] Stranded intent reconcile; double-credit idempotency on gateway ref
- [x] Insufficient wallet blocks upgrade (via shortfall) (2026-06-12)
- Note: 5 console errors on Add Funds are external Paystack SDK assets blocked in sandbox (environmental, not a bug); Paystack SDK loads eagerly on page view

## 9. Support tickets & CRM
- [x] Create ticket from portal (2026-06-12)
- [ ] Comment thread; close/reopen; empty subject/body
- [ ] Ticket-comment IDOR (B comments on A's ticket id)
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
- [ ] Revenue / tickets pages; view-as (read-only + audited)
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
- [!] `#14` Generic error templates (errors/404,400,403,409,500) hardcode "Go to dashboard" → /admin/dashboard; wrong for portal/reseller context (customer→staff login). FOLLOW-UP (cross-cutting: context-aware link or route unknown /portal & /reseller 404s to their own templates)
- [x] Backend dep down (VictoriaMetrics) → graceful (logged errors, page renders) (2026-06-12, observed)
- [!] Console errors on change-plan page — `#4` fixed PR #224
- [ ] Keyboard nav / focus / aria on forms; dark-mode; responsive breakpoints
