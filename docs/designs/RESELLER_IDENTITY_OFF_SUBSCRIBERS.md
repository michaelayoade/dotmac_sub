# Layer 3 — Reseller portal users off Subscriber rows

**Status:** Design — not started
**Date:** 2026-06-20
**Depends on / follows:** `docs/designs/IDENTITY_EMAIL_DECOUPLING.md` (Core L1+L2, PR #316)

---

## 0. Context & honest framing

Core (L1+L2) already fixed the *presenting pain*: `subscribers.email` is now
non-unique contact info, and login no longer resolves by subscriber email. So
**Layer 3 is no longer required to let a reseller's customers share an email** —
that problem is solved.

What remains is a **conceptual wart**: a reseller's portal login is still a
`Subscriber` row with `user_type=reseller`. That row is not a customer, yet it
lives in the customer table and must be explicitly filtered out everywhere. This
doc designs removing that wart — giving reseller portal users their own identity.
It is a **correctness/clarity** change, not a bug fix, and it is **invasive to
auth** (token shape, MFA, sessions). Weigh that before committing.

---

## 1. Current state (what makes a reseller "a fake customer")

- A reseller portal user is a `Subscriber` with `user_type == reseller`
  (`app/services/reseller_portal.py:180`), linked to a `Reseller` via the
  `reseller_users` table (`person_id → subscribers.id`,
  `app/models/subscriber.py:615`) and/or `Subscriber.reseller_id`.
- Login flows through the shared `auth_flow.login`; the access token's `sub` is a
  **subscriber id** (`reseller_portal.py:352`). `_session_from_access_token`
  then requires `_get_reseller_user(subscriber_id)` to exist, else 403
  (`reseller_portal.py:369`).
- The login credential is a `UserCredential` whose principal is `subscriber_id`
  (the model enforces `subscriber_id XOR system_user_id`,
  `app/models/auth.py:47`).
- Because the reseller is a subscriber, every customer query must exclude it:
  `Subscriber.user_type != UserType.reseller`
  (`reseller_portal.py:122-126,156`).
- Impersonation / "view-as" attributes to an **acting subscriber id**
  (`reseller_portal.py:1272+`).

Consequences: reseller emails sit in `subscribers.email`; resellers can leak into
customer lists if a filter is forgotten; MFA/sessions/audit all key the reseller
off a subscriber id.

## 2. Target model

Promote `ResellerUser` to a **first-class principal** — a login identity that is
NOT a subscriber:

- `reseller_users` gains its own identity columns: `email`, `full_name`,
  `is_active` (has), `last_login_at`. `person_id` (→subscribers) becomes
  **nullable + deprecated** (kept only for historical linkage during transition).
- `UserCredential` gains a third principal `reseller_user_id`; the principal
  check becomes **exactly one of** `{subscriber_id, system_user_id,
  reseller_user_id}`.
- `mfa_methods` and `sessions` gain `reseller_user_id` (they already branch on
  `subscriber_id` vs `system_user_id`).
- Access tokens for resellers carry `principal_type = "reseller_user"` and
  `sub = reseller_user_id`.

End state: a reseller login is a `ResellerUser` row → `Reseller` org; no
`Subscriber` row exists for it; the `user_type != reseller` customer-list filters
become unnecessary.

## 3. Recommended approach

**Option A — promote `ResellerUser` to a principal (recommended).** Smallest
delta from today: the table already exists and already links to `Reseller`. We
add identity columns + a credential FK and teach auth about a third principal.

Option B (a separate `reseller_login_users` table modelled on `SystemUser`) is
rejected: it duplicates `reseller_users` and the existing reseller↔org linkage.

## 4. Auth risk surface (every "reseller principal == subscriber" assumption)

| Site | Assumption today | Change |
|---|---|---|
| `auth_flow._principal_for_credential` | principal is subscriber or system_user | add `reseller_user` branch |
| `auth_flow._resolve_login_credential` | (post-core) username / system email | unchanged — still username; principal differs |
| `reseller_portal._session_from_access_token:352` | `sub` is a subscriber id | accept `reseller_user` principal; map to reseller_id directly |
| `reseller_portal._get_reseller_user` | lookup by subscriber id | lookup by reseller_user id (keep subscriber fallback during transition) |
| `mfa_methods` primary-method indexes (`auth.py`) | per subscriber / system_user | add per-reseller_user |
| `sessions` / `auth_cache` claims | principal_type ∈ {subscriber, system_user} | add reseller_user |
| `request_password_reset` | subscriber/system_user by email | add reseller_user branch |
| impersonation `acting_subscriber_id` (`reseller_portal:1272+`) | acting principal is a subscriber | attribute to reseller_user id |
| customer queries `user_type != reseller` | resellers pollute customer table | remove filter post-cutover |

## 5. Phased rollout (flag-gated, reversible)

**Phase 0 — schema (additive, idempotent).** Add `reseller_user_id` to
`user_credentials`, `mfa_methods`, `sessions`; widen the credential principal
check to a 3-way XOR; add `email`/`full_name`/`last_login_at` to `reseller_users`;
make `person_id` nullable. No behaviour change.

**Phase 1 — dual-read auth.** Teach principal resolution, session creation, MFA,
sessions, and password reset to understand a `reseller_user` principal **while
keeping** the subscriber-reseller path working (feature flag
`reseller_user_principal_enabled`, default off). Nothing cuts over yet.

**Phase 2 — backfill.** For each existing reseller subscriber
(`user_type=reseller`): ensure a `ResellerUser` row carrying its identity
(`email` ← subscriber email, `full_name`), repoint its `UserCredential` to
`reseller_user_id`, and migrate MFA methods — or force re-enrolment. Dry-run-first
one-off, in-container. Live reseller sessions: either accept old
subscriber-`sub` tokens for a grace window or force re-login at cutover.

**Phase 3 — cutover (flip flag).** New reseller logins issue
`reseller_user`-principal tokens; stop creating reseller subscriber rows at
onboarding; remove the `user_type != reseller` exclusions from customer queries;
soft-retire the reseller subscriber rows.

**Phase 4 — cleanup.** Drop the `_get_reseller_user` subscriber fallback,
`person_id`, and the `user_type == reseller` concept once no traffic uses them.

## 6. Risks & mitigations

- **Token-shape break for live reseller sessions** — biggest risk. Mitigate with
  the grace-window dual-accept in Phase 2, or a scheduled forced re-login.
- **MFA continuity** — migrate `mfa_methods` rows to the new principal or require
  re-enrolment; communicate before cutover.
- **Audit/impersonation attribution discontinuity** — keep `person_id` linkage so
  historical "acting subscriber" records still resolve.
- **Forgotten filters** — after Phase 3 the `user_type != reseller` filters become
  dead; remove them deliberately and grep for any missed customer query that
  newly (correctly) excludes nothing.
- **Reversibility** — every phase is flag-gated and additive until Phase 4; the
  flag flips back to the subscriber path.

## 7. Open questions (resolve before Phase 1)

1. Do resellers need MFA continuity, or is forced re-enrolment at cutover
   acceptable?
2. Grace-window dual-accept of old tokens vs. forced re-login — ops preference?
3. Should retired reseller subscriber rows be soft-deleted or hard-deleted after
   Phase 4 (impacts historical impersonation attribution)?
4. Is the effort justified now that Core already fixed the email problem, or park
   Layer 3 until it actively hurts?
