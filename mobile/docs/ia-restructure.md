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

### Home (lifecycle-aware)
- **Active** (service live): connection status; an **active-visit card** (live
  technician map + ETA + Track/Message) *only while a work order is
  `in_progress`*; balance/usage; Add funds.
- **Onboarding** (pre-activation): install stepper (Quote → Survey → Install →
  Activation) + the same live-visit card; retires on activation.

### Feature placement

| Feature | Domain | Today | Target |
|---|---|---|---|
| Get a quote · upgrade · add location | Sales | Profile row | **Service** ("Grow your service") + Home prompt |
| Technician visit · live map | Projects | Profile → 404 | **Home** card while `in_progress`; history under **Help → Visits** |
| Installation progress | Projects | Profile row | **Onboarding Home**, retires on activation |
| Tickets · live chat | Support | Support tab | **Help** tab (renamed) |
| Refer & Earn | Sales | Profile row | **Account** (fine — low frequency) |
| Map-pin, Payment, Contacts, Sessions, Settings, Change password | — | Profile (mixed) | **Account** (settings only) |

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

### PR 2 — Home: active-visit card + lifecycle state  *(highest value)*
- Active-visit card on Home, shown only when a work order is `in_progress`
  (reuse the technician-live-location gate). CTAs: Track on map + Message.
- Onboarding stepper state, auto-selected from install/work-order stage.
- **Accept:** active visit → card with live location; none → no card, no error;
  onboarding customer → stepper; activated → active state.

### PR 3 — Service "Grow your service" + Help "Visits"
- **Service** tab: Upgrade plan + Add-a-location/Get-a-quote entries (Sales).
- **Help** tab: a **Visits** section listing past & scheduled technician visits
  (the Projects history that used to be a Profile row).
- **Accept:** Service shows sales entries wired to existing quote/upgrade flows;
  Help lists visits from `/me/work-orders`.

### PR 4 — Account = settings only  *(cleanup, lands last)*
- Remove **Technician visits** and **Installation progress** rows from Account
  (now surfaced on Home and Help respectively).
- **Accept:** Account lists only config rows; removed features reachable from
  their new homes.

### (Separate) Bug fixes surfaced during the walkthrough
1. `GET /me/work-orders` 404-for-no-data → return `200 []` + friendly empty state.
2. Notifications feed renders raw HTML (invoice email) → strip/convert.
3. Notifications feed leaks unrendered `{{…}}` template vars → interpolate/suppress.

## 6. Out of scope (v1)
- Segment-aware "Sites/Projects" area for business/multi-site accounts (v2).
- Reseller-side nav changes (evaluate parity after customer v1 lands). The
  reseller portal is a separate grid landing (`reseller_home_screen.dart`), not
  the customer shell, so it's unaffected by PRs 1–4.
