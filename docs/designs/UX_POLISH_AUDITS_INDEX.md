# UX-polish & operator-control audits — index

A whole-app, two-track audit of the admin/customer/partner surface, run as parallel
read-only review agents per domain and synthesized into one doc per domain.

## The two tracks

- **POLISH** — make existing, working features *feel finished and trustworthy*:
  complete states (loading/empty/error/partial-success), clear feedback &
  affordances, accurate labels/freshness/units/timezone, surfaced capability,
  consistency, accessibility. No new capability, no logic change.
- **CONTROL** — expose a value/decision the code hardcodes as a setting, per-action
  option, safety mode, or override. Small-feature work (persistence + validation +
  RBAC), surfaced by the polish pass but scoped/reviewed separately.

Each doc has: synthesized themes → a P0/P1/P2 priority table → acceptance criteria →
a per-cluster appendix of every finding (`file:line` + recommend/defer).

## Domains

| Domain | Doc |
|--------|-----|
| Networking (router/RADIUS/OLT-ONT/TR069/topology/IPAM/WireGuard) | `NETWORKING_UX_POLISH_AUDIT.md` |
| Billing (invoices/payments/dunning/accounts/portal/settings) | `BILLING_UX_POLISH_AUDIT.md` |
| Catalog & services (offers/subscriptions/FUP/subscribers/requests) | `CATALOG_SERVICES_UX_POLISH_AUDIT.md` |
| Notifications & messaging | `NOTIFICATIONS_UX_POLISH_AUDIT.md` |
| Customer portal (non-billing) | `CUSTOMER_PORTAL_UX_POLISH_AUDIT.md` |
| Reseller (admin + portal) | `RESELLER_UX_POLISH_AUDIT.md` |
| CRM sync & identity | `CRM_IDENTITY_UX_POLISH_AUDIT.md` |
| Support / tickets | `SUPPORT_UX_POLISH_AUDIT.md` |
| Dashboards / reports / alerts | `REPORTS_DASHBOARDS_UX_POLISH_AUDIT.md` |
| Integrations & webhooks | `INTEGRATIONS_WEBHOOKS_UX_POLISH_AUDIT.md` |
| System / config / legal / GIS | `SYSTEM_CONFIG_UX_POLISH_AUDIT.md` |
| VAS / wallet / bill-payments | `VAS_WALLET_UX_POLISH_AUDIT.md` |
| Auth / sessions / MFA | `AUTH_SESSIONS_UX_POLISH_AUDIT.md` |

(First three were delivered in an earlier PR; the rest in this series.)

## Systemic patterns (recur across nearly every domain)

These are worth tackling as cross-cutting initiatives rather than per-domain:

1. **Dead / misleading controls** — inputs collected then ignored, settings pages
   whose keys no consumer reads, toggles that don't toggle. Examples: catalog
   bulk-tariff `start_date` + `ignore_balance`; the FUP settings page; notification
   "Active" toggle + deactivated-template-still-sends; CRM conflict-policy/ambiguous-
   match selectors; support team/routing settings with no UI; webhook event-
   subscription grid; Data Retention + Monitoring `*_warn_pct` + dead toggle groups;
   MFA recovery-code link. → **A "no dead controls" check**: every form field maps to
   a consumer; every settings key has a reader; CI can grep for orphans.

2. **Duplicated load-bearing constants that drift** ("keep in sync" comments).
   Examples: networking suspend/block address-list name (3+ copies, 2 literals);
   billing billable-status set (4+ copies incl. raw SQL); catalog FUP `0.8`; ONT
   `-25 dBm` (networking + reports); VAS top-up limits + dedupe windows; auth
   lockout/password constants. → **A single source of truth for status/policy
   constants.**

3. **Two parallel systems with divergent guarantees.** The settings control plane
   (`settings_spec` vs bespoke `web_system_config` string-writers) and the webhook
   UIs (system vs integrations: encrypted vs plaintext, working vs broken). →
   **Converge on the typed/validated/cached path.**

4. **Money & time presentation.** Hardcoded `₦` despite multi-currency / a
   `default_currency` setting, cross-currency sums, and naive-UTC timestamps with no
   tz label — in virtually every UI domain. → **Shared currency + timezone display
   helpers, used everywhere.**

5. **Bulk/async without partial-success or observability.** Bulk ops report only
   success counts (or just reload); scheduled jobs (autopay, reconcilers, GIS sync,
   CRM sync, integrity/health) surface no last-run/result in-app. → **A standard
   "bulk result {done, skipped, failed}" shape + a job-run observability surface.**

6. **Confirms scaled to blast radius.** Missing on irreversible/disruptive actions
   (money posts, mass sends, fleet pushes, impersonation, terminal transitions,
   customer-visible publishing) and over/under-used elsewhere. → **A confirm
   convention keyed to scope (single / filtered-bulk / all-customers).**

## Correctness-adjacent findings that jumped the queue

Surfaced by the polish pass but they're real bugs, not cosmetics:
- Billing: a **GET reconciliation route writes + commits** duplicate rows per page
  load; AR-aging counts drafts (overstates receivables).
- Catalog: FUP `threshold_amount` typo silently coerces to `0.0` → throttles/blocks
  **every** customer on the offer.
- Integrations: editing a system webhook **re-encrypts the ciphertext** → corrupts
  the signing secret; integrations webhook secrets are **plaintext** at rest.
- Reports: fabricated finance/retention KPIs and permanently-zero charts shown as
  real; header-only technician CSV.
- CRM: two schedulers can double-run the same pull; webhook overwrites identity with
  no audit.
- Reseller: VAS page **500s on render** (`'%,.2f'|format`).

## Out-of-scope flags (recommend a dedicated security review)

Route-guard asymmetries (mutating routes lacking `require_permission`) in
**integrations** (incl. `/hooks/{id}/test` → `subprocess.run`), **legal**, **gis**,
and **catalog_settings**; the auth MFA-recovery absence + per-worker in-memory portal
throttle + reset-token-in-redirect-URL. Verify against the mount-registry RBAC layer
first; the asymmetry itself is the finding.
