# Customer portal (non-billing) — UX-polish & operator-control audit

**Date:** 2026-06-29
**Method:** 2-agent parallel read-only review: (a) home/dashboard/profile/usage/
notifications, (b) contracts/installations/contacts/support-from-portal.
**Status:** implemented on branch `codex/customer-portal-ux-polish-audit`.
The billing/pay/invoice/top-up customer flows were covered separately in
`BILLING_UX_POLISH_AUDIT.md`; this covers everything else a customer touches.

## What this audit is

Two tracks (definition in `NETWORKING_UX_POLISH_AUDIT.md`): **POLISH** and
**CONTROL**. This is **customer-facing**, so POLISH is weighted heavily and one
finding crosses into security (read-only/view-as enforcement).

## Acceptance criteria (customer-portal-specific)

1. A read-only / reseller "view-as" session cannot mutate customer data — every
   mutating handler enforces it (parity with the payment routes).
2. Every customer-facing status/number is real (no hardcoded "all systems
   operational", no fabricated next-bill), in the right currency and timezone.
3. Every customer action shows a result (signed / saved / failed) and fails
   gracefully with friendly copy — never a raw 500.
4. No dead controls (avatar button, missing form fields) and no design-token
   regressions on legal/critical pages.
5. Self-service depth where it removes a support contact: reschedule, pay-to-
   restore, mark-as-read, notification preferences.

## Implementation update

**Updated:** 2026-07-02

### Done

- **P0 resolved:** read-only/view-as sessions are blocked from customer mutations
  across portal POST flows; support timestamp rendering is null-safe; profile
  save failures render friendly inline errors instead of false success/raw 500.
- **P1 resolved:** dashboard status and next-bill data are real, customer dates
  use the portal WAT formatter, restricted accounts get a Pay to Restore CTA,
  contract signing shows a confirmation banner, the contract signing button uses
  compiled brand tokens, notification preferences are deeper, notification inbox
  supports read/unread and mark-as-read, and contact name/relationship fields are
  available.
- **P2 resolved:** dead avatar control removed, customer-managed authority flags
  are gated behind operator control, attachment size copy is config-driven with
  client-side checks, installation status filters are whitelisted, closed tickets
  show next-step guidance, notification labels no longer expose delivery status,
  attachment sync caveat is shown, and notification defaults are aligned.

### Left

- No required P0/P1/P2 work remains from this audit.
- Optional future enhancement: replace the prefilled support-ticket reschedule/
  cancel flow with a direct appointment reschedule/cancel workflow when
  operations is ready to accept customer-side scheduling changes.

## Cross-cutting themes

### POLISH

**P-A. Read-only / view-as not enforced on mutations (security).** Non-billing
mutating POSTs — profile update, contacts create/update, WiFi change, ONT reboot,
speedtest submit — never check `customer.get("read_only")`, unlike the payment
routes and resend-verification. A reseller view-as session can edit profile/
contacts and reboot the ONT (`app/web/customer/routes.py:1197-1274`, `:929,:958,:778,:1394,:1467`).

**P-B. No-visible-result / false success / crash.**
- `customer_update_profile` always redirects `?saved=1`, no try/except; dup-email
  IntegrityError → raw 500; `updated is None` shows false "Profile updated" (`routes.py:1251-1274`)
- Post-contract-sign redirect sets `?signed=true` but nothing reads it — no
  confirmation a legal signature was recorded (`app/web/customer/contracts.py:95-101`)
- Support dates render `ticket.get('updated_at', ...)[:16]`; CRM sets those keys to
  `None` → `None[:16]` raises TypeError → **500** on any null timestamp (`templates/customer/support/index.html:64`, `crm_portal.py:182`)

**P-C. Misleading status / fabricated data.**
- Hero badge "All systems operational" (pulsing green) is hardcoded, renders even
  when service is suspended/blocked/terminated (`templates/customer/dashboard/index.html:36-43`)
- "Next Bill" card fabricates `₦0` + `now()+30d` for prepaid/no-bill customers (`app/services/customer_portal_context.py:300-309`)
- Notification inbox colors labels by **delivery** status (sent/failed) — internal
  state shown to the customer (`templates/customer/notifications/index.html:34-39`)

**P-D. Money / timezone display.** Currency hardcoded + inconsistent (`" NGN"`
suffix vs `₦` glyph) (`dashboard/index.html:71,102` vs `restricted.html:28`);
all timestamps raw UTC `strftime`, no tz label (WAT off ~1h) across dashboard,
notifications, usage, installations, support.

**P-E. Dead / broken controls & design regressions.**
- Avatar "change photo" camera button is a dead control (no handler) (`templates/customer/profile/index.html:81-86`)
- Contract-sign page uses stale Tailwind tokens whose hover/focus variants aren't
  compiled → legal e-signature button has no hover/focus state (`templates/customer/contracts/sign.html:9,11,86`)
- Contacts create/edit omit `full_name`/`relationship` though routes accept them —
  contacts can't be named (`templates/customer/contacts/index.html`) *(same pattern as the reseller portal)*

### CONTROL (customer self-service to offer)

**C-1. Self-service depth gaps that force a support contact.**
- No reschedule/cancel for installation appointments — only "please contact
  support" plain text (`templates/customer/installations/detail.html:113`)
- Blocked-for-non-payment dashboard offers only Contact-Support/Open-Ticket; no
  "Pay to restore" CTA though `outstanding_balance`/`recent_invoices` are in context
  (`templates/customer/dashboard/restricted.html:35-43`)
- Notification inbox is read-only — no read/unread, no mark-as-read, no link to
  preferences (`templates/customer/notifications/index.html`)

**C-2. Notification preferences too shallow.** Only two booleans
(`billing_notifications`, `sms_updates`, default True); no push toggle (despite a
"Push notification" contact-method option), no per-event/per-category opt-out, no
locale (`templates/customer/profile/index.html:204-249`).

**C-3. Customer-set authority flags need approval.** Customers self-designate
`is_authorized` ("speak on behalf") and billing-contact with no operator
verification (`templates/customer/contacts/index.html:142-153`).

**C-4. Config-driven copy / validation.** Attachment "5 MB" limit is hardcoded copy
in two templates vs the real `MAX_ATTACHMENT_BYTES` (drift, no client-side check)
(`templates/customer/support/new.html:68`); installations status filter has no enum
validation (bad `?status=` → empty list) (`customer_portal_context.py:686`).

## Priority

| Tier | Items | Current status |
|------|-------|----------------|
| **P0** | read-only/view-as bypass on non-billing mutations; support `None[:16]` -> 500; profile-save 500/false success | **Resolved** |
| **P1** | real dashboard status/next bill; money+tz display; contract confirmation/re-skin; reschedule/cancel entry point; pay-to-restore CTA; notification prefs + mark-as-read; contacts naming | **Resolved** |
| **P2** | avatar dead button; authority flag approval gate; attachment validation/config copy; status enum validation; closed-ticket guidance; delivery-status color leak; prefs default alignment | **Resolved** |

## Appendix — full findings

### Home / dashboard / profile / usage / notifications
- [POLISH] (High) `app/web/customer/routes.py:1197-1274` (+ `:929,958,778,1394,1467`) — non-billing mutations don't check `read_only` unlike payment routes; view-as can edit profile/contacts/WiFi/reboot ONT → gate on `read_only` [recommend]
- [POLISH] (High) `routes.py:1251-1274` — profile update no try/except; dup-email → 500; `updated is None` false success → catch ValueError/IntegrityError, re-render with inline errors [recommend]
- [POLISH] (High) `templates/customer/dashboard/index.html:36-43` — hardcoded "All systems operational" badge contradicts suspended/blocked state → drive off `service.status`/`stats_error` [recommend]
- [CONTROL] (Med) `dashboard/index.html:71,102,238,291` vs `restricted.html:28,105` — currency hardcoded + inconsistent (" NGN" vs ₦) → centralize in branding, one representation [recommend]
- [POLISH] (Med) `dashboard/index.html:105,287`; `notifications/index.html:40`; `usage/_content.html:367` — timestamps raw UTC, no tz label → convert to display tz + label [recommend]
- [CONTROL] (Med) `templates/customer/profile/index.html:204-249` — only 2 notification booleans; no push/per-event/locale → per-channel/event prefs + locale [recommend]
- [POLISH] (Med) `customer_portal_context.py:300-309` + `dashboard/index.html:99-106` — "Next Bill" fabricates ₦0/+30d for no-bill customers → hide / "No upcoming bill" [recommend]
- [POLISH] (Med) `profile/index.html:81-86` — avatar "change photo" dead control → wire upload or remove [recommend]
- [CONTROL] (Med) `templates/customer/notifications/index.html` — read-only inbox; no read/unread, mark-as-read, prefs link → add unread + mark-as-read + Manage-preferences link [recommend]
- [CONTROL] (Med) `dashboard/restricted.html:35-43,139-154` — blocked customer has no Pay-to-restore CTA → add Pay-now deep-link to pay/top-up flow [recommend]
- [POLISH] (Low) `notifications/index.html:34-39` — label color from delivery status (sent/failed) exposed to customer → color by type/severity [defer]
- [POLISH] (Low) `profile/index.html:6-8` vs `routes.py:93-94,1214-1215` — template defaults missing prefs True but form/audit treat missing as False → align default in service layer [defer]

### Contracts / installations / contacts / support-from-portal
- [POLISH] (High) `templates/customer/contracts/sign.html:9,11,86,89` — stale `primary-*`/`slate-*` tokens not compiled; no hover/focus on "Sign Agreement" (legal e-sign) → re-skin to brand/stone [recommend]
- [POLISH] (High) `app/web/customer/contracts.py:95-101` + service-orders detail — post-sign `?signed=true` unread; no signature confirmation → render "Contract signed" banner [recommend]
- [CONTROL] (High) `templates/customer/installations/detail.html:113` — no reschedule/cancel; plain-text "contact support" (no link) → add reschedule/cancel (or prefilled support link) [recommend]
- [POLISH] (High) `installations/index.html:53-62`, `detail.html:40-52` — `scheduled_start/end` tz-aware but rendered raw UTC, no tz → convert + label [recommend]
- [CONTROL] (Med) `templates/customer/contacts/index.html` (~124-217) — create/edit omit `full_name`/`relationship` though routes accept → add inputs [recommend]
- [POLISH] (Med) `support/index.html:64`, `detail.html:37,56` — `get(key,default)[:16]` but CRM sets keys to `None` → `None[:16]` TypeError → 500; raw ISO no tz → date filter with null guard [recommend]
- [POLISH] (Med) `crm_portal.py:318-320,390-392` — ticket/comment attachments local-only, may not sync to CRM, customer not told → surface note or push attachments [defer]
- [CONTROL] (Med) `contacts/index.html:142-153,197-208` — customer self-designates `is_authorized`/billing with no operator approval → gate behind approval (pending) [defer]
- [POLISH] (Med) `support/new.html:65-68`, `detail.html:77-80` — attachment size/type is copy only, no client-side check → validate before submit [defer]
- [CONTROL] (Low) `customer_portal_context.py:686` — installations status filter no enum validation; bad `?status=` → empty list → whitelist value [defer]
- [POLISH] (Low) `support/detail.html:67-93` — closed/resolved ticket hides comment form with no message/reopen → "ticket closed — start a new one" + link [defer]
- [CONTROL] (Low) `support/new.html:68`, `detail.html:80` — "5 MB" copy duplicated vs `web_support_tickets.py:190` real limit → drive from config [defer]
