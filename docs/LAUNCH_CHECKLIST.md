# Customer Onboarding Launch Checklist

Pre-launch test checklist before bringing real customers onto the platform.
Status as of **2026-06-11**. Update the checkboxes and dates as items are verified.

Legend:
- `[x]` verified working (date + how in parentheses)
- `[ ]` needs testing
- ⚠️ known issue or pending decision — resolve before launch

Suggested order of attack: §0 blockers → §3 money flows with live keys → §1 auth
round-trips → §4 change-plan/usage → one full §9 "new customer" dry run as the
final gate.

---

## 0. Blockers & decisions (do these first)

- [ ] ⚠️ Purge test catalog data: 45 of 82 active `catalog_offers` look like
      test/E2E artifacts (counted 2026-06-11) and many are customer-visible in
      change-plan/add-ons. Also 2 e2e subscribers remain. Leftovers from the
      Playwright-vs-prod incident (guard added in PR #75).
- [ ] ⚠️ Decide visibility for the 11 "odd" portal-visible offers flagged during
      offer scoping (PR #179) — product decision pending.
- [ ] ⚠️ Merge PR #187 (password-reset hardening: single-use tokens, rate
      limiting, audit events, system-user TTL cap) — reset links are a
      customer-facing attack surface.
- [ ] ⚠️ Fix customer login copy: placeholder still says "Enter your PPPoE
      username" but email/local login is supported (PR #185). Suggest
      "Username or email".
- [ ] ⚠️ Set GLITCHTIP_DSN (brand.json / env) — mobile crash reporting is
      currently off; you will be blind to app crashes during onboarding.

## 1. Auth & account access

- [x] Customer change-password applies only to the local email credential,
      never PPPoE/RADIUS (2026-06-11, verified on prod via rollback test, PR #185)
- [x] Branding/logo on all login, MFA, forgot/reset pages across customer,
      reseller and staff portals (2026-06-11, visual verification, PR #186)
- [ ] Forgot-password round-trip on a real mailbox: email arrives, link works,
      24h expiry honored, token is single-use (re-test after PR #187)
- [ ] Portal invite flow: admin customer page → invite email → set password →
      first login
- [ ] MFA: enroll, login challenge, wrong-code rejection — on all three portals
- [ ] Account lockout after 5 failed attempts, 15 min unlock; "account
      disabled" and canceled-subscriber rejections
- [ ] Session expiry/refresh, remember-me duration, logout, revoke-all-sessions
      (mobile sessions screen)
- [ ] Mobile biometric app-lock arm/resume cycle on a **real device**
      (only `flutter analyze`'d so far — lifecycle timing untested on hardware)

## 2. Service lifecycle & network

- [ ] End-to-end activation: create subscriber → provision PPPoE/RADIUS
      credentials → device connects → live session visible in admin
      (session observability deployed PR #142; reaper 1h/15min)
- [ ] Suspend → customer actually loses access (enforcement) → resume restores
- [ ] Self-service ONT reboot reaches the device (TR-069/GenieACS)
- [ ] Self-service WiFi SSID/password change reaches the device
- [ ] Live bandwidth on portal/mobile shows correct download/upload direction
      (rx=upload / tx=download NAS convention — regression-prone; use
      download_bps/upload_bps naming only)

## 3. Billing & money (highest risk)

- [x] Scheduled billing runner produces invoices (fixed + backfilled
      2026-06-09, first scheduled success verified 2026-06-10) —
      **re-verify on the next cycle before launch**
- [ ] Invoice correctness on a real plan: amounts, tax, due date, PDF renders
      with branding
- [ ] Paystack with **live keys**: pay an invoice end-to-end including verify
      callback and webhook → invoice marked paid, ledger entry created
- [ ] Flutterwave with **live keys**: same end-to-end
- [ ] Top-up flow (portal + mobile); balance reflects immediately
- [ ] Autopay: save card, enable/disable, charge fires on due date
- [ ] Bank-transfer proof upload → admin verification → payment applied
- [ ] Payment arrangements: create, installment charge, cancel
- [ ] Failure paths: declined card, abandoned webview, double-submit,
      webhook retry idempotency
- [ ] Dunning experience: what a non-paying customer sees (restricted
      dashboard, captive redirect), and recovery after payment

## 4. Plans, add-ons, usage

- [ ] ⚠️ Change-plan page performance: previously built ~80 proration quotes
      upfront → 46s load / 504 / app crash. A lazy `/change/quote` endpoint
      now exists — explicitly verify page load is fast and submit works
- [ ] Add-on / data-bundle purchase with payment; appears in usage
      immediately; bundle-expiry push notification fires
- [ ] Offer visibility scoping: each reseller's customers see only their
      offers (plan_family + reseller availability, PR #179)
- [ ] Usage accuracy: portal/mobile usage-summary vs RADIUS accounting on a
      known-traffic test line
- [ ] FUP: approaching/exceeded banners at correct thresholds (portal +
      mobile card); quota enforcement actually throttles/blocks

## 5. Support & notifications

- [ ] Ticket create/comment from portal and mobile → appears in CRM;
      CRM-side reply → visible to customer (bidirectional sync verified
      2026-06-10 — re-smoke before launch)
- [ ] Email deliverability from prod: SPF/DKIM/DMARC for dotmac.ng senders;
      spam-folder check on Gmail and Outlook
- [ ] Push notifications on real Android + iOS devices (FCM): usage alerts,
      bundle expiry
- [ ] SMS channel if enabled (Twilio config)
- [ ] All transactional emails carry branding and correct expiry wording (24h)

## 6. Mobile release readiness

- [ ] Release builds with prod `--dart-define-from-file` — brand.json
      API_BASE_URL currently points at `10.0.2.2:8000` (emulator value)
- [ ] Store metadata, signed builds, app identity (applicationId / bundle id)
- [ ] Payment deep-link scheme `dotmacpay` returns from gateway webview
- [ ] Token refresh after the app sits idle for days; forced-logout UX when a
      session is revoked server-side

## 7. Security & privacy

- [x] RBAC mount-registry guards: 290 customer-reachable staff routes closed,
      build-failing arch test (PRs #178/#181/#182/#183, deployed 2026-06-10)
- [ ] Run /security-review on the accumulated diff since the RBAC overhaul;
      resolve pending P1b endpoint-scoping decisions
- [ ] Cookies `secure`/`httponly`/`samesite` verified behind the real TLS
      domain; HSTS at the proxy
- [ ] IDOR sweep: customer A cannot read customer B's invoices / tickets /
      usage / arrangements by ID-swapping `/portal/*/{id}` and `/me/*` URLs
- [ ] PPPoE password reveal endpoint (`/admin/customers/person/{id}/pppoe-password`)
      restricted to the intended staff role (currently `customer:read` —
      confirm that is the intended bar)
- [ ] Rate limits on login / forgot-password / speedtest endpoints
      (forgot-password lands with PR #187)

## 8. Performance & capacity

- [x] Monitoring dashboard ~6–10s steady (PRs #85/#86); ingestion OOM fixed —
      no create_engine/Redis-client creation in task hot paths (PR #163)
- [ ] Dashboard / billing / usage pages under realistic concurrency
      (e.g. 200 customers) — note `WEB_CONCURRENCY` currently defaults to
      **1 uvicorn worker**
- [ ] Celery queue depth during an invoice run + notification burst

## 9. Ops & onboarding mechanics

- [ ] **Database backups + a tested restore** (the working tree IS prod via
      bind mount — a bad deploy or stray `rm` is unrecoverable without them)
- [x] Alembic: single head, migrations run as explicit pre-deploy step
      (`make docker-migrate`), never on boot
- [ ] Written deploy runbook: merge → pull on main → migrate → restart the
      11 services that bind-mount `./app` (app, 7 celery workers,
      celery-beat, bandwidth-poller, syslog-listener)
- [ ] Uptime/error alerting for the portal itself (Zabbix watches network
      devices; who watches the app?)
- [ ] Bulk onboarding path: import/create N subscribers + PPPoE creds, batch
      portal invites, RADIUS sync (`scripts/migration/populate_radius_from_subs.py`)
- [ ] Full "new customer" dry run as the final gate: create → invite →
      login (web + mobile) → pay first invoice → consume data → raise
      ticket → receive notifications
