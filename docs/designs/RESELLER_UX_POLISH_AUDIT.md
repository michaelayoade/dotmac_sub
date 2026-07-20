# Reseller (admin + partner portal) — UX-polish & operator-control audit

> **Status: historical audit evidence.** Revalidate unresolved recommendations against `docs/UI_INFORMATION_AND_ACTION_STANDARD.md` and the current domain SOT before implementation.

**Date:** 2026-06-29
**Method:** 2-agent parallel read-only review: (a) admin reseller management
(list/detail/form/user-linking/impersonation), (b) the partner-facing reseller
portal (dashboard/billing/reports/contacts/profile).
**Status:** implementation branch in progress. `codex/reseller-ux-polish-audit`
addresses the concrete P0/P1/P2 findings that already had backing fields or
clear UI/service behavior. Structural product/data-model decisions remain
explicitly pending below.

> Known: a reseller parity loop already shipped revenue/tickets/profile+MFA/
> billing-pay/fiber-map/view-as/service-requests/bank-transfer-proofs. Findings
> build on that.

## What this audit is

Two tracks (definition in `NETWORKING_UX_POLISH_AUDIT.md`): **POLISH** and
**CONTROL**. The reseller domain adds a strong structural CONTROL gap: **partner
economics (commission/markup/credit/payout) are absent from both the data model and
the UI** — today it's "policy by absence."

## Acceptance criteria (reseller-specific)

1. A deactivated reseller stays visible and re-activatable; lifecycle isn't a
   one-way trap.
2. Money shown to a partner carries its currency and isn't summed across
   currencies; dates carry a timezone.
3. Financial/impersonation actions (allocate credit and view-as) are
   confirmed and (for allocation) amount-controllable.
4. No partner-facing page render-crashes; capabilities aren't hidden from nav.
5. Reseller economics are explicit and configurable, not implied by missing rows.

## Cross-cutting themes

### POLISH

**P-A. Render crash / broken / dead controls.**
- Deactivating a reseller makes it **vanish** (list hardcodes `is_active=True`,
  no status filter, no reactivate path); the "Inactive" badge branch is dead code
  (`app/services/web_admin_resellers.py:98,102`, `templates/admin/resellers/index.html:37`)
- Contacts page silently empty for a `reseller_user` with no backing subscriber —
  the `notice` is set but the template doesn't render it (`app/services/web_reseller_contacts.py:61`)
- Deactivate is only the form's "Active" checkbox (which then hides the row); no
  explicit activate/deactivate action anywhere (`templates/admin/resellers/index.html:41`)

**P-B. Money / timezone display.**
- Admin billing card hardcodes `₦` while invoices/payments below render
  `{{ currency }}`; `outstanding_balance` sums `balance_due` across all currencies
  → mixed-currency mislabeled as naira (`templates/admin/resellers/detail.html:44`)
- Portal revenue totals rendered with no currency symbol/code at all
  (`templates/reseller/reports/revenue.html:33,50,147`)
- Admin + portal datetimes are raw UTC `strftime`, no tz label (WAT off by ~1h)

**P-C. Partial-success / error surfacing.**
- Reseller-user created but portal invite email fails → only `logger.warning`,
  route 303s as success (`app/services/web_admin_resellers.py:383-385`)
- `reseller_account_status_update` maps every `ValueError` to a generic message,
  discarding the service's real validation/enforcement reason (`app/services/web_reseller_routes.py:279-289`)

**P-D. Confirms on financial / sensitive actions.**
- **Allocate** (portal) dumps the *entire* unallocated credit onto one subscriber,
  no amount picker, no confirm (`templates/reseller/billing/index.html:215-223`)
- "View as reseller" impersonation is one-click, no confirm, mints a real session
  (`templates/admin/resellers/detail.html:13-22`)

**P-E. Hidden capability / dead params.**
- Contacts has no primary-nav entry (desktop or mobile); reachable only via Profile
  (`templates/layouts/reseller.html:43-86`)
- Contact create/edit forms omit `full_name`/`relationship` though the route
  accepts them (`templates/reseller/contacts/index.html:149`)
- Dashboard threads `page`/`per_page`/`offset` but renders no pager (`app/web/reseller/routes.py:32-39`)

### CONTROL

**C-1. Partner economics absent (structural).** The `Reseller` model has no
commission/markup, credit-limit, or payout-terms fields, and nothing exposes them;
commission is display-only (`commission_total`) with no rate visible/configurable
(`app/models/subscriber.py:118-166`). → product/data-model decision: per-reseller
fields (commission %, credit limit) + global defaults in settings; scope before UI.

**C-2. Catalog visibility default-open (unstated policy).** With no
`OfferResellerAvailability` rows a reseller implicitly sees **all** active offers
(`app/services/web_admin_resellers.py:730-750`). → per-reseller "restrict to
assigned offers" flag + global `RESELLER_DEFAULT_CATALOG_OPEN` default.

**C-3. Existing override unexposed.** `Reseller.policy_set_id` (per-reseller dunning
override) exists on the model but appears in no form/detail (`app/models/subscriber.py:145`).
→ add a policy-set selector.

**C-4. Role assignment.** Detail user-create assigns no role / omits the role
selector the new-reseller form has (`app/web/admin/resellers.py:283-310`).

## Priority

| Tier | Items |
|------|-------|
| **P0** | Deactivated reseller vanishes + can't reactivate (`web_admin_resellers.py:98`); Allocate dumps entire credit, no amount/confirm (`billing/index.html:215`) |
| **P1** | Reseller economics surface + decision (C-1); confirms on allocate / view-as (P-D); money+tz display (P-B); invite-failure + status-update error surfacing (P-C); Contacts in nav + name fields (P-E); catalog-visibility default flag (C-2) |
| **P2** | per_page consistency, dashboard pager, `policy_set_id` selector (C-3), role selector on detail user-create (C-4), detail not-found notice |

## Implementation update — 2026-07-01

### Resolved in `codex/reseller-ux-polish-audit`

**P0 required**
- Stopped inactive resellers from becoming unreachable: admin list now supports
  `active` / `inactive` / `all` filters and explicit deactivate/reactivate
  actions.
- Reworked reseller credit allocation so the portal requires an allocation
  amount, confirms the action, and passes the requested cap through to
  `allocate_consolidated_balance_to_subscriber`.

**P1 concrete**
- Added confirmations for allocate and view-as-reseller.
- Surfaced reseller account status `ValueError` messages instead of replacing
  every validation failure with a generic unsupported-action message.
- Added Contacts and Profile to the reseller desktop/mobile navigation.
- Rendered the contacts unavailable notice and exposed `full_name` plus
  `relationship` fields already accepted by the routes.
- Added currency labels to reseller revenue summary totals/chart/table.
- Changed the admin reseller billing card from hardcoded naira display to
  per-currency outstanding balances.
- Added explicit `UTC` labels to audited admin/portal timestamp renders.
- Created a partial-success path for reseller creation when portal invite email
  fails: the reseller/user can remain created while the detail page displays the
  invite issue.

**P2 concrete**
- Added dashboard pagination controls for the recent-accounts list.
- Unified reseller detail user-link/create fallback `per_page` defaults to 25.
- Exposed `Reseller.policy_set_id` through the admin reseller form and detail
  page.
- Added a role selector to detail-page reseller portal user creation.
- Added a detail not-found notice on redirect back to the reseller list.

### Still pending

- **C-1 partner economics** remains a product/data-model decision. The current
  model still has no commission rate, markup, credit-limit, payout terms, or
  global defaults to expose safely.
- **C-2 catalog visibility default flag** remains pending. There is still no
  per-reseller "restrict to assigned offers" flag or global
  `RESELLER_DEFAULT_CATALOG_OPEN` setting to wire into offer visibility.
- **Full timezone conversion helper** remains pending. This branch labels the
  audited raw timestamps as `UTC`, but does not introduce a shared app-timezone
  conversion helper for all admin/portal timestamps.

## Appendix — full findings

### Admin reseller management
- [POLISH] (High) `app/services/web_admin_resellers.py:98,102` — list hardcodes `is_active=True`, no status filter; deactivated reseller vanishes, no reactivate; dead "Inactive" badge → status filter + stop hard-filtering [recommend]
- [POLISH] (High) `templates/admin/resellers/detail.html:44` — billing card hardcodes `₦`; `outstanding_balance` sums across currencies → render currency consistently / aggregate per-currency [recommend]
- [POLISH] (Med) `web_admin_resellers.py:383-385,417-421,844-848` — invite email failure only logged, route 303s success → partial-success banner ("user created, invite failed — resend") [recommend]
- [POLISH] (Med) `templates/admin/resellers/detail.html:13-22` — "View as reseller" one-click, no confirm, mints session → confirm dialog [recommend]
- [POLISH] (Med) `templates/admin/resellers/index.html:41-46` — only View/Edit; no deactivate/delete action (buried in form checkbox) → explicit activate/deactivate w/ confirm [recommend]
- [POLISH] (Low) `app/web/admin/resellers.py:230-231` — not-found silently redirects to list → "Reseller not found" notice [defer]
- [POLISH] (Low) `detail.html:174,190,220,243` — dates UTC `strftime`, no tz → app tz/format helper [defer]
- [POLISH] (Low) `app/web/admin/resellers.py:245` vs `:221,280` — `per_page` 50 vs 25 inconsistent → unify [defer]
- [CONTROL] (Med) `app/models/subscriber.py:145` + `reseller_form.html` — `policy_set_id` exists but unexposed → add policy-set selector [recommend]
- [CONTROL] (Med) `web_admin_resellers.py:730-750` + `detail.html:259-265` — catalog visibility default-open (no rows → all offers) → per-reseller restrict flag + global default [recommend]
- [CONTROL] (Med) `app/models/subscriber.py:118-166` — no commission/markup/credit/payout fields; economics by absence → per-reseller fields + global defaults; scope first [defer]
- [CONTROL] (Low) `templates/admin/resellers/detail.html:298-321` + `app/web/admin/resellers.py:283-310` — detail user-create omits Role selector, assigns none → add role select (default configurable) [defer]

### Reseller portal (partner-facing)
- [POLISH] (Med) `templates/reseller/reports/revenue.html:33,50,147` — totals no currency symbol/code → prefix account currency [recommend]
- [CONTROL] (Med) `templates/reseller/billing/index.html:215-223` + `reseller_portal_billing.py:294` — "Allocate" dumps entire unallocated credit on one subscriber, no amount/confirm → amount field / per-invoice + confirm [recommend]
- [POLISH] (Med) `billing/index.html:255,288` + `dashboard/index.html:176` — datetimes raw UTC, no tz → convert/label consistently [defer]
- [POLISH] (Med) `templates/layouts/reseller.html:43-86,172-197` — Contacts not in nav (desktop or mobile) → add Contacts + Profile to nav incl. mobile [recommend]
- [POLISH] (Med) `app/services/web_reseller_contacts.py:61` vs `templates/reseller/contacts/index.html` — `notice` set for reseller_user w/o subscriber but template doesn't render it → render notice banner [recommend]
- [CONTROL] (Low) `templates/reseller/contacts/index.html:149,94` vs `routes.py:383,438` — create/edit omit `full_name`/`relationship` though route accepts → add inputs [recommend]
- [POLISH] (Low) `app/web/reseller/routes.py:32-39` + dashboard — `page`/`per_page`/`offset` threaded but no pager → add pager or drop params + label "recent" [defer]
- [POLISH] (Low) `app/services/web_reseller_routes.py:279-289` — status-update maps every ValueError to generic message, discards real reason → surface `str(exc)` [defer]
- Notes: login/MFA/account danger-zone well done (spinner/confirm/modal/CSRF); commission display-only — rate not visible/configurable (ties to C-1).
