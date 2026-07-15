# UI Edge-Case Test Catalog

Tracked checklist of edge cases to exercise through the UI (admin, customer, reseller
portals) on a **disposable local stack** — never prod (see
`playwright-e2e-local-stack` notes). Tick items with date + PR as they're verified.

Legend: `[x]` verified OK · `[!]` bug found (see ref) · `[ ]` not yet tested.

Bugs found so far are tracked in **PR #224** unless noted. `#N` refers to the
running finding numbers from the 2026-06-12 driving session.

---

## 1. Auth, session & MFA
- [x] Login wrong password ×N → account lockout (verified by review 2026-06-13): DB-backed per-credential lockout (`auth_flow._record_login_failure`) — 5 fails → 15-min `locked_until`; lock checked BEFORE password verify (no correctness oracle, locked accounts answer identically), attempts-while-locked don't extend the lock (403 before recording), expired lock starts a fresh window, success resets `failed_login_attempts` + clears lock. Both RADIUS and local providers record failures. Per-IP login throttle (`login_rate_limit_middleware`) is in-memory per-worker (weaker under WEB_CONCURRENCY>1) but the per-account DB lockout is the real protection — sound. (Note: "Account locked" 403 reveals the account is locked — minor, accepted.)
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
- [!] `#31` (prepaid/postpaid model review 2026-06-13) `billing_mode` is denormalized on Subscriber(account)/Subscription/Offer/OfferVersion with NO enforced sync — and only `Subscription.billing_mode` is load-bearing (invoice generation is mode-blind; collections/enforcement dispatch on the subscription field; `Subscriber.billing_mode` is read only by CRM push / portal display / `_apply_billing_defaults` seed / create-time inheritance). Latent gaps: no propagation on account-mode change, no subscribe-time offer-vs-account guard, mixed-mode account silently skips prepaid enforcement, no audit. PARTIAL FIX shipped: new-subscription import no longer hardcodes prepaid and now inherits the subscriber's migrated `billing_mode`; added `billing_mode_audit.find_billing_mode_inconsistencies` + `scripts/one_off/audit_billing_mode.py` flagging subscription_vs_account / subscription_vs_offer / mixed_mode_account drift, unit-tested. REMAINING (scope w/ product): subscribe-time mode guard, account→subscription propagation, mixed-mode prepaid-enforcement gap. See the 2026-06-13 review for the full trace

## 5. Change-plan & proration
- [x] Upgrade quote + shortfall enforcement (2026-06-12)
- [x] Downgrade applies with family set (2026-06-12)
- [!] `#5` heading "Upgrade Summary" for downgrades — fixed PR #224
- [!] `#6` downgrade "₦0 remaining value" misleading — fixed PR #224
- [!] `#7` empty-family instant change 400 — fixed PR #224
- [!] `#30` (found on PROD by driving account 100000016) Self-service plan change **ignored account debt** and the two paths disagreed: (a) the affordability check used credit-only `get_account_credit_balance` (overdue invoices invisible); (b) the gate was **prepaid-only** — postpaid/indebted accounts applied with NO balance/debt check; (c) the web path auto-applied while the mobile/`/me` API path only queued a pending request ("request sent"). FIXED 2026-06-13 (decision: block-until-settled + auto-apply both paths): `apply_instant_plan_change` AND `submit_change_plan` now block with a clear "overdue balance" 400 if `get_account_outstanding_balance` (overdue arrears) > 0, for prepaid AND postpaid; the `/me` API path now routes through `apply_instant_plan_change` (auto-applies same-family when eligible; cross-family → migration ticket; insufficient prepaid funds → 402). Unit-tested (arrears blocks postpaid change + leaves offer unchanged; no-arrears postpaid auto-applies). Page notice DONE: `get_change_plan_page`/error-context now expose `in_arrears`+`arrears_amount`; the change-plan page shows a red "Settle your balance to change plans (overdue ₦X)" banner with a Pay-now CTA and hides the plan picker while in arrears (enforcement at submit still backstops it)
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
- [x] `#28` (money/rate upper bounds, extends #27) Payment/credit `amount`+totals (`Numeric(12,2)`) and tax `rate` (`Numeric(6,4)`) schemas enforced `ge=0`/`gt=0` but no maximum — a huge value passed validation then overflowed the column at commit (`DataError`; now a 400 via #24 rollback but with an ugly raw-DB message). FIXED 2026-06-13: added upper bounds — payment/credit amounts `lt=10_000_000_000`, tax rate `lt=100` — across PaymentCreate/Update, CreditNoteCreate/Update, TaxRateCreate/Update, so a too-large value is a clean `ValidationError` (clear message, no DB round-trip). Unit-tested (9,999,999,999.99 & rate 7.5 accepted; ≥ bound, negative rejected); billing submodule suite passes. Non-numeric amount is already rejected (Decimal parse → 400); wrong-currency-vs-invoice still untested
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
- [!] `#25` Reseller service-request create guard was bypassable with whitespace: `if not subscriber_id and not (contact_name and contact_phone)` checked the RAW strings' truthiness, but the values were `.strip() or None` on storage — so `contact_name="   "` + `contact_phone="   "` passed the guard yet stored as `(None, None)`, creating a contactless request (the exact thing the guard blocks). Same whitespace class as #18/#20. FIXED 2026-06-13: `create_request` strips name/phone/email/address/notes UP FRONT, then validates, so whitespace-only is treated as empty (400 "Provide an existing customer or lead contact name + phone"). Unit-tested (whitespace → error redirect, no row); coord parsing was already robust
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
- [x] `#29` Double-submit money forms → no duplicate (2026-06-13): **gateway** payments are deduped by the `uq_payments_active_external_id` partial unique index (+ `uq_payment_provider_events_external_id` for webhooks) — a customer double-submitting an online payment can't double-charge. Wallet `pay_bill` has a 60s identical-debit guard. **GAP FOUND+FIXED**: manually-recorded admin/offline payments have no `external_id`/`provider_id`, so the index didn't cover them — a double-clicked "record payment" created two rows (over-credit). `Payments.create` now rejects an identical manual payment (same account + amount, no external_id/provider_id, active) recorded in the last 60s with a 409 (mirrors `pay_bill`). Unit-tested (dup manual → 409; different amount → allowed). Tradeoff: two genuinely-distinct same-amount manual payments within 60s are blocked (refresh + retry) — acceptable, matches the wallet guard
- [ ] Two admins editing same customer/subscription
- [ ] Wallet read-modify-write race under concurrent debits
- [ ] Idempotent task re-runs (invoice cycle, autopay) don't double-charge

## 16. Empty / loading / error states & a11y
- [x] Several list empty states (customers/offers/invoices/tickets) (2026-06-12)
- [x] Unknown URL → branded 404 (no stack trace), shows Reference ID (2026-06-12)
- [x] `#14` Error handler now routes /portal & /reseller errors to their branded templates (correct dashboard link), not just /admin — fixed PR #224 (_template_response). Note: customer/reseller lack some status-code templates (403/409/500) and still fall back to the generic admin-linked one — a minor residual
- [x] Backend dep down (VictoriaMetrics) → graceful (logged errors, page renders) (2026-06-12, observed)
- [!] Console errors on change-plan page — `#4` fixed PR #224
- [x] `#24` (SYSTEMIC — AUDIT COMPLETE 2026-06-13) Several admin POST handlers had a generic `except Exception:` fallback that re-queries the DB (`get_sidebar_stats`, `_get_subscriber`, `build_*_context`, etc.) and re-renders a form WITHOUT first calling `db.rollback()`. If the caught error left the transaction aborted (any IntegrityError/DataError), those follow-up queries fail on the poisoned session → **500 instead of a clean 400/409**. This is exactly the #19 root cause. 6 parallel analysis agents traced each candidate's write path to the specific constraint + checked rollback: of ~30 candidates, **19 REAL/PARTIAL** fixed by inserting `db.rollback()` as the first statement of the `except` (converting a 500 into the handler's existing clean 4xx form re-render). FIXED sites — billing_accounts 119/202 (bad tax_rate_id/reseller_id FK), billing_credits 101 (amount overflows Numeric(12,2)), billing_reporting 63 (rate overflows Numeric(6,4)), billing_payments 379/554 (amount overflow), notifications 157/242/525/619/762/846 (dup template code+channel / alert-policy name / on-call-rotation name), system 1763/1850/1960/2038/2223/2292 (dup role name / permission key / webhook url) + 2140 (over-length api-key label), customers 1490/1552 (delete generic fallback). Earlier confirmed+fixed instances: #19 (person/business edit), #26 (create_address/create_contact). Live-verified: duplicate notification template → 400 form re-render (was 500). FALSE sites left untouched (already protected by service-layer rollback): billing_invoice_actions 111/157 + billing_invoices 353/401/490 (InvoiceLines/CreditNotes roll back before re-raise — latent only if a header-level unique constraint is ever added), system 458 (savepoint-isolated import) + 1255 (MFA service self-heals). Follow-up DONE: added `app/db.form_write(db)` — a `with form_write(db): <write>` context manager that rolls the session back on error before the handler's `except` re-renders (can't be forgotten like an inline `db.rollback()`); applied to the 10 catalog_settings create/update handlers (closes the #27 rollback gap). New form handlers should use it; existing inline `db.rollback()` fixes stand
- [x] `#26` (subset of #24, FIXED) `create_contact` (customers.py:1747) and `create_address` (1617) had the worst variant: their `except Exception:` rendered the literal `admin/errors/500.html` at status **500** for ANY error (even a recoverable bad id / validation), and re-queried `get_sidebar_stats(db)` on a possibly-poisoned session with no rollback. FIXED 2026-06-13: both now `db.rollback()` and split handling — `IntegrityError`→409, `ValueError`→400 (clean HX toast via `_htmx_error_response` or an `HTTPException`), unexpected→500. Success path untouched. Exception-contract unit test added (`create_customer_contact` bad UUID → ValueError, which the handler maps to 400)
- [!] `#27` (#24 coverage gap — NOTED, defense-in-depth) `app/web/admin/catalog.py` and `catalog_settings.py` were OUTSIDE the #24 audit's candidate grep. Both have **zero `db.rollback()`** and the same shape: a form POST whose `except Exception` sets an error and falls through to a shared `_base_context(db)` (→ `get_sidebar_stats(db)`) re-render — so a transaction-aborting error would 500 on the poisoned re-query. BUT unlike the #24 REAL hits, the catalog_settings models (RegionZone/PolicySet/UsageAllowance/SlaProfile) have **no unique constraints** and no obvious user-controllable overflow/FK trigger, so a poisoning error isn't clearly reachable → latent, not confirmed. Sites (catalog_settings.py `except Exception`): region_zone 159/209, usage_allowance 317/376, sla_profile 482/533, policy_set 646/696, add_on 809/859 (+ deletes 234/401/558/719/882). Recommend folding the rollback gap into the same shared rollback-then-render helper as #24 (not patched — reachability unconfirmed, catalog under active development). **FIXED part: offer/add-on price upper bound** — `OfferPriceCreate/Update` + `AddOnPriceCreate/Update` `amount` now `Field(gt=0, lt=100_000_000)` so a huge price (which would overflow `Numeric(10,2)`) is a clean validation 400/422 instead of a 500. Verified at the schema layer (8000.00 & 99999999.99 accepted; ≥1e8, negative, zero rejected; same on update)
- [ ] Keyboard nav / focus / aria on forms; dark-mode; responsive breakpoints
