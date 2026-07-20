# Selfcare App — Information Architecture Restructure

**App:** `dotmac_portal` (`io.dotmac.selfcare`) · **Status:** in progress
**Interactive mockup:** https://claude.ai/code/artifact/81090248-588a-4fc4-bb98-9525302ac512

---

## 1. Problem

Profile became an **overflow drawer**. The bottom nav is a fixed 5 tabs
(Home · Billing · Service · Support · Profile); every feature that didn't
obviously map to Billing/Service/Support got appended as a Profile `ListTile`.
Result:

- **Settings and live service events are mixed** — "Technician visits" and
  "Installation progress" (transient, time-sensitive) sit next to "Change
  password" and "Settings".
- **Discoverability fails for the highest-value flows** — a customer whose
  technician is en route won't dig into Profile → row 6. That row also hits a
  dead-end: `GET /me/work-orders` returns **404 for no-data**, surfaced as
  "Something went wrong (404)".
- **Account wastes a bottom-tab slot** — a low-frequency destination competes
  with daily jobs and crowds the bar.

## 2. Principles

1. **Domains are a backend concern.** Sales / Projects / Support model the CRM,
   not customer-facing tabs. Mapping them 1:1 to the tab bar exposes the org
   chart.
2. **The bottom bar is for recurring jobs.** Identity & settings live in the
   **header** (top-right avatar), not a tab — the pattern every mature app
   (banking, delivery, messaging) uses.
3. **Home is lifecycle-aware.** Sales & projects surface as **contextual cards**
   where the customer's job is; Home adapts to the customer's stage (onboarding
   vs active). The app auto-detects stage; it is never a user-facing toggle.
4. **Segment-aware later** (out of scope v1): residential gets the lifecycle
   Home; business/multi-site customers may warrant a real "Sites/Projects" area.

## 3. Target structure

### Bottom nav: 5 → 4 tabs
`Home · Service · Billing · Help` (Support → **Help**; Profile removed).

### Header
`DOTMAC` wordmark (left) · notification bell + **avatar** (right). Avatar →
Account (`/profile`), pushed above the shell.

### Home (calm, with transient status banners)
Home stays a normal dashboard. Transient, time-bound events surface as **slim
banners** that link to their own screens — they never take Home over:
- **Active-visit banner** (accented): while a work order is `in_progress` —
  "Technician on the way · $tech · $eta ›" → the full tracking map.
- **Installation-progress banner** (muted/secondary): while an install is under
  way. Onboarding is a *one-time* activity, so it's deliberately low-key — not
  as pronounced as going-concern surfaces (support, service, billing). It links
  to the install tracker and disappears once complete.

### Feature placement

| Feature | Domain | Today | Target |
|---|---|---|---|
| Get a quote · upgrade · add location | Sales | Profile row | **Service** ("Grow your service") + Home prompt |
| Technician visit · live map | Projects | Profile → 404 | Slim **Home banner** → full map on its own screen (`/track/:id`); also in **Help → Visits**. The map is *not* embedded on Home. |
| Installation progress | Projects | Profile row | **Muted Home banner** (one-time; low-key) → install tracker; retires on activation |
| Tickets · live chat | Support | Support tab | **Help** tab (renamed) |
| Refer & Earn | Sales | Profile row | **Account** (fine — low frequency) |
| Map-pin, Payment, Contacts, Sessions, Settings, Change password | — | Profile (mixed) | **Account** (settings only) |

### Copy decision — the account-activity tab
The Billing account-activity view is a **live running record with a balance** —
that's a *ledger*, not a *statement* (a statement is a periodic snapshot). Label
it for the audience: **"Activity"** on the customer app/web (consumer-standard,
matches banking/fintech apps), **"Ledger"** on the reseller app/web (business
audience; precise). The data model is `LedgerEntry` either way. ("Statement"
stays reserved for a future downloadable periodic document.)

## 4. Implementation map (key files)

| Concern | File |
|---|---|
| Bottom-nav scaffold (`_tabs`, `NavigationBar`) | `lib/src/features/home/home_shell.dart` |
| Router (`StatefulShellRoute.indexedStack`, top-level routes) | `lib/src/router/app_router.dart` |
| Header avatar button | `lib/src/widgets/account_avatar_button.dart` |
| Account (was Profile) screen + menu | `lib/src/features/auth/profile_screen.dart` |
| Home / dashboard | `lib/src/features/home/dashboard_screen.dart` |
| Work orders (technician visits) | `lib/src/features/profile/work_orders_screen.dart`, `lib/src/repositories/work_order_repository.dart` |
| Installation tracker | `lib/src/features/profile/installation_tracker_screen.dart` |
| Notifications feed | `lib/src/features/home/notifications_screen.dart` |
| Shared async/error view | `lib/src/widgets/async_value_view.dart` |

## 5. PR sequence (each independently shippable)

Ordered so **no feature ever becomes unreachable** — the Account cleanup that
removes the Technician-visits / Installation rows lands **last**, after their
new homes exist.

### PR 1 — Nav shell: 4 tabs + header avatar  ✅ *(this PR)*
- Bottom nav 5 → 4 (`Home · Service · Billing · Help`); `Support` label → `Help`.
- `AccountAvatarButton` added to all four tab app bars → opens `/profile`.
- `/profile` (+ all sub-routes) moved from a shell branch to a **top-level
  route** above the shell — opens full-screen with a back button; every
  `/profile/*` deep link and `context.push` is unchanged.
- Account screen title `Profile` → `Account`. **All Account rows kept** (nothing
  moved yet).
- **Accept:** app builds; 4 tabs render; avatar opens Account; every old Profile
  sub-screen still reachable; route paths (`/support`, `/usage`, `/billing`,
  `/dashboard`) unchanged so notification deep links keep working.

### PR 2 — Home: active-visit banner  ✅ *(this PR)*
- A **slim visit banner** on Home (deliberately *not* an embedded map) shown
  only when a work order is `in_progress`: "Technician on the way · $tech ·
  $eta ›". It routes to the full tracking screen (`/track/:id`, the existing
  `TechnicianTrackScreen`). The heavy live map stays on its own screen so the
  dashboard stays calm — the map is also reached from **Help → Visits** (PR 3).
- **Accept:** in-progress visit → banner appears and opens the live map; no
  active visit → no banner, no error.

### PR 2b — Onboarding banner  ✅ *(folded into PR 4)*
- **Not** a Home takeover. Onboarding is a one-time activity, so it's a **muted,
  secondary banner** on Home (via `projectsProvider`, shown while an install's
  `progressPct < 100`) that links to the install tracker and retires on
  completion — far less pronounced than the accented visit banner.

### PR 3 — Service "Grow your service" + Help "Visits" + reseller Ledger  ✅
- **Service** tab: a "Grow your service" section header over the existing
  Get-a-quote (add-location) + Upgrade entries (Sales — already present).
- **Help** tab: a third segment **Visits** (Tickets · Chat · Visits) rendering
  the technician visits. `WorkOrdersScreen` body extracted to a reusable
  `WorkOrdersView` (used by both `/profile/technician-visits` and Help).
- **Reseller** billing label → **"Ledger"** (mobile). *Follow-up:* the reseller
  web billing label wasn't found in this checkout — relabel when located.
- **Accept:** Help shows Tickets/Chat/Visits; Visits lists `/me/work-orders`
  with Track (in-progress) and Rate (completed); reseller mobile shows "Ledger".

### PR 4 — Account = settings only (+ onboarding banner)  ✅
- Remove **Technician visits** and **Installation progress** rows from Account —
  now surfaced in **Help → Visits** and a **muted Home banner** respectively.
- Adds the onboarding banner (PR 2b, folded in) so Installation keeps a Home
  entry point exactly as it leaves Account (routes `/profile/*` still exist).
- **Accept:** Account lists only config rows; both features reachable from their
  new homes; Account is now purely settings/identity.

### (Separate) Bug fixes surfaced during the walkthrough
1. `GET /me/work-orders` 404-for-no-data → return `200 []` + friendly empty state.
2. Notifications feed renders raw HTML (invoice email) → strip/convert.
3. Notifications feed leaks unrendered `{{…}}` template vars → interpolate/suppress.

## 6. Out of scope (v1)
- Segment-aware "Sites/Projects" area for business/multi-site accounts (v2).
- Reseller-side nav changes (evaluate parity after customer v1 lands). The
  reseller portal is a separate grid landing (`reseller_home_screen.dart`), not
  the customer shell, so it's unaffected by PRs 1–4.
