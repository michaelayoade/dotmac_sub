# Bundled Service System — Design

**Date:** 2026-07-02
**Status:** Draft (design approved, spec under review)
**Owner:** billing / enforcement

## Problem

A customer's service is delivered as multiple subscriptions — a base internet
plan plus riders like a `/29` IP block, and soon **voice**. Today those pieces
are modeled two incompatible ways, and neither is complete:

| Model | Enforcement | Billing |
|---|---|---|
| **Standalone subscription** (64 IPs) | ❌ diverges from base | ✅ generates the invoice line |
| **Thin add-on** (`subscription_add_ons`, 101 IPs) | ✅ atomic with parent | ❌ not billed (billing code never reads it) |

Consequences observed in production (2026-07-02):

- **Enforcement divergence** — a debtor's base internet stays `active` while the
  `/29` sub is `suspended` (e.g. `100025479`, 176 days overdue, suspend logged,
  base still authenticating). RADIUS auth is per-subscriber (one username), so a
  single active member keeps the customer online despite the account owing.
- **Double-modeling** — 23 accounts have the same IP as *both* a standalone sub
  and an add-on.
- **Unbilled inventory** — ~78 add-on-only IPs generate no charge.

There is no single unit that says "these services belong together and live or
die as one."

## Goal

A **first-class bundle**: a base service plus its components (IP blocks, voice,
future services) form one unit that is **enforced, suspended, restored, and
expired atomically** — no member can diverge. Generic by design so Voice and
later products slot in without rework.

## Non-goals (YAGNI)

- **No add-on billing rebuild.** Components stay standalone subscriptions, which
  already bill correctly. The thin `add_ons`/`subscription_add_ons` model is
  retired for billable components.
- **No catalog "bundle product" / single-package price.** Itemized-grouped
  billing is the decision; a named sellable bundle at one price is a later,
  optional layer.
- **No IP re-pricing.** ₦2,500/IP is confirmed correct (`/29` = ₦20k). Untouched.
- **No change to dedicated internet (DIA) handling** beyond making the existing
  hands-off exclusion explicit at the bundle level.

## Design

### 1. Data model

New table **`subscription_bundles`**:

| column | notes |
|---|---|
| `id` | PK |
| `subscriber_id` | FK → subscribers; all members share it |
| `label` | human name, e.g. "Business — 100 Mbps + /29" |
| `anchor_subscription_id` | the base internet sub; the bundle's network/RADIUS identity |
| `is_dedicated` | cached: true if any member's offer `plan_family = 'dedicated'` |
| `status` | derived from members (see §2) |
| `is_active`, `created_at`, `updated_at` | standard |

**`subscriptions.bundle_id`** — nullable FK → `subscription_bundles`.
A subscription belongs to **at most one** bundle. Non-bundled subs keep
`bundle_id = NULL` and behave exactly as today.

Invariants:
- All members of a bundle share `subscriber_id`.
- Exactly one member is the anchor (`anchor_subscription_id`), and it is the
  base internet service.
- `is_dedicated` is recomputed on membership change.

### 2. Lifecycle & enforcement (the core value)

Bundle-atomic operations wrap the existing per-subscription lifecycle:

- `suspend_bundle(bundle_id, reason, source)` → suspends **every** member via the
  existing `account_lifecycle.suspend_subscription`.
- `restore_bundle(...)`, `expire_bundle(...)` → same, all members.

Enforcement decision (dunning reconciler) operates at **bundle granularity**:
when the account is a genuine net debtor past its policy grace **and the bundle
is not dedicated**, it suspends the whole bundle. Restore restores the whole
bundle. This is a thin wrapper over today's `_suspend_account` (which already
loops all collectible subs) — the bundle makes the grouping *explicit and
guaranteed* rather than incidental.

**Divergence invariant:** all members of a bundle share the same enforcement
state. A reconciler guard detects any member whose state disagrees with the
bundle/anchor and converges it. The `100025479` partial state (base active + IP
suspended) becomes structurally impossible.

**DIA exclusion:** `is_dedicated` bundles are hands-off for auto-enforcement
(contract/SLA-managed). Made explicit at the bundle level.

**RADIUS:** unchanged mechanism (per-subscriber username). Because a debtor
bundle now suspends *all* members, `compute_account_status` resolves the
subscriber to `suspended` → the username is rejected → the multi-sub
authentication gap closes.

`compute_account_status` continues to derive subscriber status from subscription
states; no change needed beyond members now moving together.

### 3. Billing (unchanged)

Components bill per-subscription onto the **account-level invoice**, which
already carries `subscription_id` per line — i.e. itemized-grouped billing
already exists. A bundle adds **no** billing code. Suspended-bundle billing
follows existing suspended-subscription behavior.

### 4. Migration

Phased, reversible, no customer-facing change until enforcement flips:

1. **Backfill bundles** — for each account with a base internet sub + standalone
   IP sub(s), create a `subscription_bundles` row (anchor = base internet) and
   set `bundle_id` on the members. Single-service accounts get no bundle
   (`bundle_id` stays NULL).
2. **Dedupe double-modeling** — for the 23 both-modeled accounts, keep the
   **billed standalone sub** as the bundle component and retire the vestigial,
   unbilled add-on record.
3. **Unbilled add-on-only IPs (~78)** — OPEN QUESTION (see below): comp or bill.
   Not resolved by this migration; tracked separately.
4. **New provisioning** — IP/voice provisioning creates a component subscription
   and attaches it to the customer's bundle (creating the bundle if the base
   exists without one). New code path replaces thin-add-on creation for billable
   components.

### 5. Testing

- `suspend_bundle`/`restore_bundle` move **all** members atomically.
- Reconciler heals a manually-diverged member back to the bundle state.
- A dedicated (`is_dedicated`) bundle is excluded from auto-enforcement.
- Billing still emits one itemized line per component on the account invoice.
- Migration groups base+IP correctly, dedupes a double-modeled account, leaves
  single-service accounts unbundled.
- A debtor bundle suspension results in the subscriber RADIUS username rejected.

## Rollout phases

1. **Model + lifecycle** — schema + `*_bundle` operations + reconciler invariant.
   No behavior change (nothing bundled yet).
2. **Migration** — backfill bundles for existing multi-sub accounts; dedupe.
3. **Enforcement flip** — dunning/reconciler enforces at bundle granularity; DIA
   bundles excluded.
4. **Provisioning** — new IP/voice provisioning uses the bundle path.

## Open questions

- **~78 unbilled add-on-only IPs** — intentional comps or missed billing? If
  billed, they become standalone-sub components at ₦2,500/IP. Needs the owner's
  call; handled as a separate remediation, not blocking this design.
- **Voice component specifics** — DID/provisioning attributes are defined when
  Voice integrates; the bundle model is agnostic to component service type.

## Impact

- Closes the multi-sub enforcement gap (debtors can no longer ride an active
  sibling subscription).
- Eliminates the divergence and double-modeling classes.
- Gives Voice and future services a ready home — add a component subscription to
  the bundle; enforcement/billing/lifecycle come for free.
