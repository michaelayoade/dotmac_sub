# Auth / sessions / MFA — UX-polish & operator-control audit

**Date:** 2026-06-29
**Method:** single-agent read-only review of auth/sessions/MFA/login/invite through a
UX-polish + operator-control lens (**not** a full security review).
**Status:** audit only. Part of the remaining-module audit series.

> The agent's verdict: a **narrow security review is warranted** — specifically the
> MFA recovery-code absence, the per-worker in-memory portal throttle, and the
> reset-token-in-redirect-URL pattern (`web_auth.py:196`). Those are out of the
> POLISH/CONTROL scope and listed only as a pointer.

## What this audit is

Two tracks (definition in `NETWORKING_UX_POLISH_AUDIT.md`). The cluster is mature —
TTLs, customer lockout, session caps, cookie security are already `settings_spec`
(auth domain). The gaps are (a) the **admin/system-user** path still uses hardcoded
lockout/MFA constants while the customer path is configurable (drift), (b) a **dead
MFA "recovery code" affordance** with no backing route/codes, (c) thin cooldown/
loading feedback.

## Acceptance criteria (auth-specific)

1. A TOTP-locked admin has a real self-service recovery path (or the affordance is
   removed) — no dead links.
2. Lockout/MFA/password policy is configurable from one source for admin *and*
   customer (no drift).
3. Lockout messages state the remaining cooldown; auth forms have loading/disabled
   states.
4. Auth-policy values live once (no password-min in two places, no copy hardcoding a
   configurable TTL).

## Cross-cutting themes

### POLISH

**P-A. Dead / broken control (locks people out).**
- "Use a recovery code" links to `/auth/mfa/recovery` — **no such route/template/
  code-generation exists** → 404; a TOTP-locked admin has no self-service recovery
  (only admin-initiated disable) (`templates/auth/mfa.html:67`)
- MFA enrollment shows **no recovery/backup codes** — users enable TOTP with no
  fallback (compounds the dead link) (`templates/auth/mfa_enroll.html`)

**P-B. Feedback gaps.**
- Lockout messages never state remaining time though `locked_until` is known
  ("Account locked. Please try again later.") (`web_auth.py:879`, `web_customer_auth.py:305,340`)
- Forgot-password forms have no loading/disabled submit state (login + reset already
  do) → double-submit possible (`templates/auth/forgot-password.html:56`)
- Customer auth templates lack the loading states the admin equivalents have
  (inconsistent across portals)
- No self-service "active sessions / log out other devices" view (rows exist in
  `AuthSession`) — only current-session logout + admin revoke-all

### CONTROL

**C-1. Admin-vs-customer policy drift.** `LOGIN_MAX_FAILED_ATTEMPTS=5`,
`LOGIN_LOCKOUT_MINUTES=15`, `MFA_MAX_FAILED_ATTEMPTS=5`, `MFA_LOCKOUT_MINUTES=15` are
hardcoded constants for admin/system-user/reseller, while the customer path reads
`customer_login_max_attempts`/`customer_lockout_minutes` from `settings_spec` — same
defaults via two sources; admin lockout isn't operator-tunable (`app/services/auth_flow.py:745-757`).

**C-2. Unregistered keys bypass the spec.** MFA enforcement reads `force_2fa` then
`admin_mfa_required` via `_setting_value`, but **neither is registered in
`settings_spec`** (no UI/default/validation) and checking two key names is ambiguous
(`auth_flow.py:171-175`). → register one canonical `admin_mfa_required`.

**C-3. Duplicated / hardcoded policy.** Password min `< 8` hardcoded in the
validator and again client-side in templates (`auth_flow.py:1669`,
`reset-password.html:55,75`); "Remember me for 30 days" copy hardcoded vs the
configurable TTL (`templates/auth/login.html:229`); RADIUS/PPPoE portal throttle
`limit=10, window=900` hardcoded + in-memory per-worker (`web_customer_auth.py:334-338`).

## Priority

| Tier | Items |
|------|-------|
| **P0** | MFA recovery dead link + no backup codes → TOTP-locked admin has no self-service recovery (P-A) — fix-or-remove + implement recovery codes |
| **P1** | Admin lockout/MFA constants → settings (C-1, drift); register `admin_mfa_required` in spec (C-2); lockout messages state time-remaining (P-B); forgot-password loading state (P-B) |
| **P2** | password-min setting (C-3); remember-me copy from setting; active-sessions / sign-out-everywhere view (P-B); customer-template loading parity; portal throttle as settings + distributed limiter (security review) |

## Appendix — full findings
- [POLISH] (High) `templates/auth/mfa.html:67` — "Use a recovery code" → `/auth/mfa/recovery` doesn't exist (404); no recovery/backup codes anywhere → remove link or implement recovery codes + route [recommend]
- [CONTROL] (High) `app/services/auth_flow.py:745-757` — admin/system-user/reseller lockout+MFA constants hardcoded while customer path is settings-backed (drift) → auth-domain settings read by both helpers [recommend]
- [CONTROL] (Med) `auth_flow.py:171-175` — `force_2fa`/`admin_mfa_required` not registered in settings_spec (no UI/default/validation), two-key ambiguity → register one canonical `admin_mfa_required` [recommend]
- [POLISH] (Med) `web_auth.py:879`, `web_customer_auth.py:305,340` — lockout message omits remaining time though `locked_until` known → "try again in N minutes" [recommend]
- [CONTROL] (Med) `auth_flow.py:1669` + `reset-password.html:55,75` — password min `<8` hardcoded in 2+ places → `auth.password_min_length` setting threaded to validator + templates [defer]
- [POLISH] (Med) `templates/auth/login.html:229` + `reseller/auth/login.html:132` — "Remember me for 30 days" hardcoded vs configurable TTL → render from setting or neutral copy [defer]
- [POLISH] (Med) no active-sessions view (admin/reseller/customer) — only current logout + admin revoke-all; `AuthSession` rows exist → sessions list + "sign out everywhere" in profile [defer]
- [POLISH] (Med) `templates/auth/forgot-password.html:56` + customer forgot-password — no loading/disabled submit state → add `x-data` spinner pattern [recommend]
- [POLISH] (Low) `templates/customer/auth/{login,mfa}.html` — lack loading/disabled states present on admin → align spinner pattern [defer]
- [POLISH] (Low) `templates/auth/mfa_enroll.html` — no recovery/backup codes shown at enrollment → generate + display one-time recovery codes [defer]
- [CONTROL] (Low) `web_customer_auth.py:334-338` — RADIUS/PPPoE portal throttle `limit=10, window=900` hardcoded, in-memory per-worker → settings + note distributed-limiter gap (security review) [defer]
- Verified: TTLs, customer lockout, session caps, cookie security already settings-backed (auth domain).
