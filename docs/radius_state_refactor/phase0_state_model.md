# Phase 0 — RADIUS Access-State Model

**Status**: draft (phase 0 of the multi-phase refactor)
**Owner**: TBD
**Last updated**: 2026-05-26
**Supersedes**: nothing yet; collapses behaviors currently spread across
`app/services/radius.py`, `app/services/radius_reject.py`,
`app/services/enforcement.py`, and
`app/services/events/handlers/enforcement.py`.

## 1. Problem

Subscriber network access state is currently encoded in **four physical places** that we keep in sync via event handlers:

1. `subscription.status` (app DB enum)
2. `radcheck` rows (FreeRADIUS DB) — password + optional `Auth-Type := Reject`
3. `radreply` rows (FreeRADIUS DB) — IP, bandwidth, copied from catalog
4. Mikrotik `/ip firewall address-list` entries (per-customer, written via SSH)

Five reject CIDR pools (`blocked`, `negative`, `bad_mac`, `bad_password`, `not_found`) each have standing filter chains and address-lists, and the subscriber's `ipv4_address` is rewritten into the chosen pool on suspension, with the original IP stashed in a `DomainSetting` JSON blob for restore.

This is workable but accreted. Blocking touches all four state stores; restoring has to reverse all four; drift between them is detectable only by manual inspection. Per-customer SSH writes to NAS are chatty and brittle.

## 2. Target

One source of truth:

```
subscription.access_state ∈ {active, suspended, captive, terminated}
            │
            ▼
   radusergroup (one row per user: maps user → group)
            │
            ▼
   radgroupcheck (per-group: Auth-Type for suspended)
   radgroupreply (per-group: IP-pool name, bandwidth, captive routing)
            │
            ▼
   NAS standing rules per pool CIDR (configured at NAS provisioning,
   never per customer)
```

Block/unblock collapses to:

```python
def set_subscription_access_state(db, sub_id, state: AccessState):
    """Single entry point. UPSERTs one radusergroup row + optionally CoA-kicks
    live sessions. No per-customer SSH. No IP rewriting. No restore tracking."""
```

## 3. State definitions

| state | meaning | radusergroup | live session action |
|---|---|---|---|
| `active` | normal paying customer | `dotmac-active` | none (no change needed) |
| `suspended` | hard block (overdue, fraud, admin, FUP) | `dotmac-suspended` | CoA-Disconnect → SSH `/ppp active remove` fallback |
| `captive` | soft block; can reach portal only | `dotmac-captive` | CoA-Update to swap IP → fallback to disconnect+reauth |
| `terminated` | cancelled or expired; full removal | (no row) | CoA-Disconnect; user-not-found on re-auth |

Status enum in app code:

```python
class AccessState(enum.Enum):
    active = "active"
    suspended = "suspended"
    captive = "captive"
    terminated = "terminated"
```

Mapping from existing `SubscriptionStatus`:

| SubscriptionStatus | AccessState |
|---|---|
| `active` | `active` |
| `suspended`, `blocked`, `stopped` | `suspended` (or `captive` if `subscriber.captive_redirect_enabled`) |
| `canceled`, `expired`, `disabled` | `terminated` |
| `pending`, `hidden`, `archived` | (no radusergroup row; treat as not-yet-provisioned) |

## 4. Group definitions

Provisioned once via Phase 1 migration; not touched per customer afterward.

### `dotmac-active`

```
radgroupreply:
  Service-Type        := Framed-User
  Framed-Protocol     := PPP
  Mikrotik-Address-List := dotmac-active   (informational, for ops queries)
```

IP and bandwidth come from per-user `radreply` overrides during phases 1-8. Migrated to group-level `Framed-Pool` and Mikrotik-Rate-Limit in phase 9 (optional).

### `dotmac-suspended`

```
radgroupcheck:
  Auth-Type := Reject
```

That's it. User can't auth. No IP issued. No CoA needed because there's no live session permitted.

### `dotmac-captive`

```
radgroupreply:
  Service-Type        := Framed-User
  Framed-Protocol     := PPP
  Framed-Pool         := dotmac-captive-pool
  Mikrotik-Rate-Limit := 1M/1M
  Mikrotik-Address-List := dotmac-captive
```

Captive pool CIDR has standing `dst-nat tcp/80 → portal_ip` rule on each NAS.

### (no group for terminated)

`radusergroup` simply has no row for terminated users. No `radcheck` either. FreeRADIUS returns `Access-Reject` with reason "User not found" — clean audit trail.

## 5. Attr precedence

FreeRADIUS default behavior:

1. `radcheck` (per-user) — evaluated first
2. `radusergroup` → `radgroupcheck` — evaluated in priority order
3. Reply attrs: `radreply` (per-user) **overrides** `radgroupreply`
4. Group reply uses `+=` to append, `:=` to replace

**Implication**: during phases 3-7, per-user `radreply` still works and overrides group attrs. Stripping per-user `radreply` (phase 7) is the moment groups become authoritative. Plan radclient verification *between* phase 6 and 7.

## 6. NAS pool naming convention

| Pool | Purpose | CIDR (suggested) | Standing rules |
|---|---|---|---|
| `dotmac-active-pool` | active customers | configured per-NAS, existing scheme | normal forward chain |
| `dotmac-captive-pool` | captive (negative balance) | one /24 per NAS | `dst-nat tcp/80 → portal_ip`; DNS allowed; rest dropped |
| `dotmac-suspended-pool` | (unused — suspended users don't get IPs) | n/a | n/a |

Suspended users don't need an IP pool because their auth is rejected before NAS allocates one. This is one fewer pool than today.

## 7. Migration order (high level — phases detailed separately)

1. Phase 1: provision groups + pools (additive)
2. Phase 2: add `access_state` column
3. Phase 3: dual-write groups (shadow)
4. Phase 4: backfill one customer
5. Phase 5: backfill all
6. Phase 6: verify
7. Phase 7: cut over to group-level reply attrs
8. Phase 8: stop per-customer SSH writes
9. Phase 9: pool-based IP assignment (optional, deferred decision)
10. Phase 10: decommission old code

Each phase has its own document in this directory.

## 8. Intentional non-goals

| Not changing | Why |
|---|---|
| `SubscriptionStatus` enum | Business meaning is broader than network access (billing, lifecycle, reporting). `access_state` is derived from it, not a replacement. |
| `RadiusUser` table | Stays as a cache/index for the admin UI, becomes read-only-from-RADIUS view by phase 10. |
| Live session disconnect (`disconnect_subscription_sessions`) | Already simplified in the recent CoA + SSH-pool work. Keep. |
| Captive portal product feature | Same business behavior; only the implementation collapses to a group. |
| Mikrotik vendor specifics | Other vendors can map their own group→attr equivalents later. |
| Splynx import / sync paths | They populate `SubscriptionStatus`; `access_state` is derived. Splynx-aware operators see no change. |

## 9. Invariants (must always hold)

1. If `subscription.access_state IS NOT NULL` then exactly one `radusergroup` row exists for the username (or zero for `terminated`).
2. `radusergroup.groupname` ∈ {`dotmac-active`, `dotmac-suspended`, `dotmac-captive`} or no row.
3. `radusergroup` is updated atomically with `access_state` (same transaction in app DB; eventual consistency to external RADIUS DB via the dual-write path).
4. The `set_subscription_access_state` function is the only writer of `radusergroup` after phase 3.
5. Group-level attrs are immutable per environment after phase 1; changes happen via a code-reviewed migration, never per customer.

## 10. API sketch

```python
# app/services/radius_access_state.py  (new module, phase 3)

class AccessState(enum.Enum):
    active = "active"
    suspended = "suspended"
    captive = "captive"
    terminated = "terminated"

_GROUP_FOR_STATE = {
    AccessState.active: "dotmac-active",
    AccessState.suspended: "dotmac-suspended",
    AccessState.captive: "dotmac-captive",
    AccessState.terminated: None,  # no group row
}

def set_subscription_access_state(
    db: Session,
    subscription_id: str,
    state: AccessState,
    *,
    kick_sessions: bool = True,
) -> None:
    """Idempotent. Writes app DB + external RADIUS DB in one logical
    operation. Optionally CoA-disconnects live sessions for suspend/
    terminate transitions."""
    ...

def derive_access_state(
    subscription_status: SubscriptionStatus,
    *,
    captive_redirect_enabled: bool,
) -> AccessState:
    """Pure function; subscription.status → AccessState."""
    ...
```

The event handlers stop orchestrating multi-step block sequences and just call `set_subscription_access_state(db, sub_id, derive_access_state(sub.status, captive_redirect_enabled=...))`.

## 11. Rollback story per phase

Documented in each phase doc. Common pattern:

- Phases 1-6: revertible by deleting the new rows (groups, radusergroup entries) and dropping `access_state` column.
- Phase 7: revertible by re-enabling per-user `radreply` writes and re-running `_external_sync_users` to repopulate.
- Phase 8: revertible by re-enabling the address-list write call sites.
- Phase 9 (if undertaken): the only one that's not cheaply revertible. Requires IP-pool teardown + re-assigning per-customer IPs from backup. Document the rollback in its own runbook before merging.

## 12. Decisions (formerly open questions)

- **Q1** — `captive` is **derived**, not a `SubscriptionStatus` value. Computed from `subscriber.captive_redirect_enabled + status=suspended`. Keeps `SubscriptionStatus` enum stable and Splynx-aligned. Decided 2026-05-26.
- **Q2** — Access state is **shared across all credentials** of a subscription. State lives on the subscription, not the credential. Multi-credential subscribers (rare) get one state that applies to all their usernames. Decided 2026-05-26.
- **Q3** — The existing 5 reject pools **stay running** through phase 8 as belt-and-suspenders. Standing rules at each NAS are cheap to leave in place. Decommissioned in phase 9 only if/when we commit to the IP-pool migration. Decided 2026-05-26.
- **Q4** — NAS-side pool provisioning is **manual** (per-NAS by the operator), not scripted. Phase 1 doc supplies the exact RouterOS commands as a runbook. Decided 2026-05-26.

## 13. Validation checklist before approving phase 1

- [ ] All groups (`dotmac-active`, `dotmac-suspended`, `dotmac-captive`) compile in radclient against a staging FreeRADIUS
- [ ] Captive pool's `dst-nat` rule confirmed on at least one staging Mikrotik
- [ ] Existing customer 100025610 auths exactly as before (zero behavioral change with shadow not yet wired)
- [ ] DESIGN.md is updated to reference this doc
- [ ] Owner agreed
- [ ] Rollback for phase 1 ("delete the new groups") tested in staging
