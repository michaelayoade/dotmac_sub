# Service lifecycle & bundle integrity — block → reactivate, IP persistence

Date: 2026-06-17 · Status: **step-2a COMPLETE — IPAM repaired vs prod, drift 593→1 (the 1 is the
lone multi-service sub needing the service-grain fix). No served IP changed. Gauge now ~0 = live
detector. Step-2b (RADIUS projection rewrite) = deliberate post-launch. See §5b.**

## Summary

A customer's "service" is **not** a first-class bundle object — it is an assembly of
loosely-coupled records that mutate on independent lifecycles. The block → reactivate
cycle is correct on the happy path (the IPv4 address genuinely survives), but the design
has no transactional envelope and no reconcile-before-reactivate step. The real exposure
is therefore **silent partial desync** — `subscription.status = active` while one of
{external `radreply` Framed-IP, NAS profile, address-list entry} is stale — not the
"IP lost on block" symptom that prompted this review. The historic IP-loss vector exists
in the code but is disabled.

This doc records the as-built lifecycle and the integrity risks, then sets the strategic
direction (§5): **one idempotent reconciler as the sole writer of connectivity state**, sequenced
audit-gauge-first since this is pre-launch hardening, not a live incident.

## 1. There is no bundle object

`PlanCategory.bundle` (`app/models/catalog.py:115`) is a label on an offer, nothing more.
A live service is assembled from records keyed differently and with different lifecycles:

| Part | Where | Keyed by | Lifecycle driver |
| --- | --- | --- | --- |
| **Subscription** (anchor: status, offer FK, NAS FK, profile FK, `ipv4_address`/`ipv6_address` string cols) | `catalog.py:734` | `subscription_id` | status machine |
| **AccessCredential** (RADIUS user/secret) | `catalog.py:1136` | **`subscriber_id`** (not subscription) | independent |
| **IPAssignment** (pool allocation) | `network.py:498` | **`subscriber_id` + `ip_version`** | `is_active` flag |
| **SubscriptionAddOn** / data top-ups | `catalog.py:836` | `subscription_id` | **time-windowed** `start_at`/`end_at` |
| **QuotaBucket** | (per-sub) | `subscription_id` | independent |
| External RADIUS rows (`radcheck`/`radreply`/`radusergroup`) | FreeRADIUS DB | `username` | rebuilt by sync |

Consequence: **no single record is authoritative for the running service**, so the parts
drift. The IPv4 address alone lives in **three** places that must be kept in step by hand:

1. `subscription.ipv4_address` — **load-bearing**: it is what `build_radius_reply_attributes`
   writes into `radreply` as `Framed-IP-Address`.
2. The `IPAssignment` pool row — the only place with `is_active` (allocate/release) semantics.
3. The external `radreply` row — the value the NAS actually enforces.

Add-ons survive block/reactivate for free, because they are time-windowed (`start_at`/`end_at`),
not status-dependent. They are never suspended or re-evaluated by the lifecycle.

## 2. The lifecycle as built

All status mutations go through `app/services/account_lifecycle.py` — the single chokepoint
(locks the row, creates/resolves an `EnforcementLock`, emits exactly one canonical event,
recomputes the derived subscriber status). States:

```
pending ──activate──▶ active ⇄ suspended / blocked / stopped ──▶ canceled / expired / disabled
```

`derive_access_state` (`radius_access_state.py`) maps status plus the effective
persisted restriction → RADIUS group: active → `dotmac-active`; blocked-family
→ `dotmac-suspended` (hard reject by default) or `dotmac-captive` only after the
residential opt-in/readiness policy validates a captive lock; terminated → no row.

### Block / suspend
`suspend_subscription` (`account_lifecycle.py:102`) only transitions status + creates the lock.
The connectivity side-effects run **separately** (not atomic), event-driven off the
`subscription_suspended` event.

**Traced 2026-06-17 — the real production payment-suspension path** is:

```
dunning: collections/_core.py:306 ─▶ account_lifecycle.suspend_subscription
   ─▶ emits subscription_suspended
   ─▶ events/handlers/enforcement.py:_enforce_subscription_block (:101)
        ├─ enforce_subscription_reject_ip  → disabled no-op, IP untouched
        ├─ enqueue refresh_radius_from_subs  ← the SOLE writer of radcheck/radreply
        ├─ _shadow_write_access_state (radusergroup; no-op unless group_routing_enabled)
        └─ disconnect_subscription_sessions + apply_subscription_address_list_block
```

The actual radcheck/radreply mutation happens in the **single-writer status-aware sweep**
`_external_sync_users` (`radius.py:1201-1225`). For a `suspended` sub it **DELETEs radcheck +
radreply + radusergroup, then inserts a single `Auth-Type := Reject` row**. The active branch
(`:1227+`) rebuilds Framed-IP **from `subscription.ipv4_address`** via
`build_radius_reply_attributes`.

The two other block helpers are **not** on this path: `block_external_radius_credentials`
(`radius.py:1814`, non-destructive) is dead for the event path (removed under the 2026-06-11
single-writer decision — see comment at `enforcement.py:138-144`); `cleanup_subscription_on_suspend`
(`enforcement.py:1114`, destructive) is only called from the admin catalog path
(`catalog/subscriptions.py:378`).

No path clears `subscription.ipv4_address` on suspend. **Only `cleanup_subscription_on_cancel`
(`enforcement.py:1067`) nulls `ipv4_address`/`ipv6_address`** — i.e. only permanent cancel.

### Reactivate / restore
`restore_subscription` (`account_lifecycle.py:228`) has **one** precondition — status must be in
the suspended-equivalent set (`:260`). It resolves the matching enforcement locks (via the
`ALLOWED_RESTORERS` map, so a "payment" trigger clears an `overdue` lock but not a `fraud` lock),
and if no active lock remains, flips status → active and emits `subscription_resumed`. It does
**not** inspect provisioning state, IP assignment, or RADIUS readiness.

`subscription_resumed` fans out to the **same provisioning handler as activation**
(`events/handlers/provisioning.py`): (1) ensure IP assignments → (2) RADIUS sync →
(3) NAS SSH push → (4) close service orders. IP step reuses the inactive `IPAssignment`
(`provisioning_helpers.py:220-238`) so the customer gets the **same address back**.

## 3. The disabled IP-loss vector (historic)

`radius_reject.py` once swapped a blocked subscriber onto a captive/reject-pool IP: it stashed the
real IP into a runtime-state JSON blob (`original_ipv4`) and **overwrote
`subscription.ipv4_address = assigned_ip`** (`:317`). If `original_ipv4` was never captured
(double-block, or the IP was already a reject IP), the real IP was unrecoverable.

That swap is **disabled**: `radius_reject.py:265` early-returns `noop_block_swap_disabled` before
the swap code at `:267-330` ever runs. Cause documented inline: reject pools (10.11–10.14/16) have
no NAT/redirect on the BNGs, so a swapped customer is black-holed and can't even reach the pay page
(incident: cust 100025880 pinned to `10.11.112.38`). The **restore branch (`:234-251`) is kept
live** so any subscriber historically swapped still recovers their original IP on reactivation.

## 4. Integrity risks (ranked)

**R1 — Silent partial desync (highest).** Block/reactivate side-effects are not atomic with the
status change, each provisioning step is individually try/excepted, and there is no rollback. A
failure in step 3 (NAS push) leaves steps 1–2 applied: `status = active`, RADIUS says active, but
the NAS never got the profile — or the inverse. Reactivation re-runs the chain from whatever
degraded state exists; it never reconciles desired-vs-actual first. NAS SSH push is **not reliably
idempotent** (re-pushing an already-created user is vendor-dependent). Symptom: connected-but-wrong,
or walled-but-marked-active, with no alert.

**R2 — `subscription.ipv4_address` is a single point of failure (CONFIRMED, dominant risk).** The
traced prod suspension path (the single-writer sweep, `radius.py:1201-1225`) **DELETEs the external
`radreply` row for every suspended sub**, so while suspended the customer's Framed-IP exists **only**
in this one string column; restore rebuilds it from there. Nothing structurally protects the column
— it is a convention, not a constraint. Any stray write (an erroneous cancel-cleanup, a bad sync, a
re-enabled reject-swap) orphans the IP permanently, and the external row is already gone so there is
nothing to recover from. Framed-IP should derive from the `IPAssignment` row (the record with
allocate/release semantics), not the bare column.

**R3 — Reactivation has no provisioning-state awareness.** A subscriber suspended *mid-provisioning*
can be reactivated with no detection of the in-flight state; the handler blindly re-runs activation.

## 5. Strategic direction (decided 2026-06-17)

The strategic framing is **not** "add three fixes" and **not** "build a `Bundle` aggregate." It is
a single architectural rule:

> **Status transitions mutate desired state only. One idempotent reconciler is the sole writer of
> connectivity state — external `radcheck`/`radreply`/`radusergroup`, NAS profile, IP assignment,
> MikroTik address-list — and it runs on every transition AND on a periodic audit.**

This subsumes the tactical fixes: "reconcile not re-provision" *is* the rule; "IPAssignment as
Framed-IP truth" is just what the reconciler reads; a provisioning-state field is just the
reconciler's bookkeeping. They stop being separate work.

**Deliberately rejected as the first move:**
- A persisted `Bundle`/service-spec ORM aggregate — a large migration on ~27k subscribers that just
  adds a *fourth* place for state to drift. The "bundle" we want is a **desired-state view the
  reconciler computes**, not a new table. Revisit only if service composition grows (multi-line,
  multi-IP, complex add-on interactions).
- A k8s-style control plane — over-engineered for this scale/churn. The win is **consolidating the
  writers we already have** (`block_external_radius_credentials`, `cleanup_subscription_on_suspend`,
  the activation handler, `radius_reject`) behind one function. A refactor, not a rewrite.

### Sequencing (pre-launch hardening — measure before refactor)

1. **Audit gauge first — BUILT (2026-06-17).** `app/services/ip_consistency_audit.py` +
   `app.tasks.radius.audit_ip_consistency` (beat: `radius_ip_consistency_audit`, 6h, read-only) +
   metrics collector exporting `radius_ip_consistency_drift{kind}` and
   `radius_ip_consistency_population`. Tests: `tests/test_ip_consistency_audit.py` (12, green).
   Compares the three IPv4 sources for every active sub expected to carry a pinned IP; drift classes:
   `assignment_missing` (core R2), `assignment_mismatch`, `radreply_missing`, `radreply_mismatch`,
   `radreply_orphan`. Suspended subs excluded (their radreply is deleted by design).
   **Run once vs prod** (no code-path change, just reads):
   `docker compose exec app python scripts/one_off/audit_ip_consistency.py --store`
   — `--store` also lights up the /metrics gauge. This quantifies real drift before touching writers.
2. **Consolidate writers.** A single-writer for radcheck/radreply **already exists**
   (`_external_sync_users`, the 2026-06-11 decision) — so this step is narrower than first thought:
   pull the **still-scattered** writers (IP assignment, NAS SSH push, MikroTik address-list) under
   the same single-writer discipline as one idempotent reconciler, the *only* function permitted to
   mutate external connectivity state. Route activate, resume, suspend, and the audit's auto-repair
   through it.

   **IP-dimension increment — BUILT (2026-06-17), shadow, not yet wired.**
   `app/services/connectivity_reconciler.py`:
   `converge_subscription_connectivity(db, subscription_id, *, apply=False)`. Establishes the IPAM
   `IPAssignment` as the IPv4 source of truth and converges the subscription column (and, via the
   existing sweep, the external Framed-IP) to it. **Defaults to shadow** (`apply=False` → returns a
   plan, writes nothing); `apply=True` sets the column to the IPAM address and enqueues the
   single-writer refresh; idempotent; `backfill_ipam` (column set, no IPAM row — the
   `assignment_missing` case) is report-only, never auto-applied. Active subs only (suspended subs'
   radreply is deleted by design). Tests: `tests/test_connectivity_reconciler.py` (7, green). NOT
   wired into the live enforcement/provisioning handlers yet — that cutover, plus NAS-profile and
   address-list convergence, waits until the audit (step 1) quantifies real drift.
3. **Make the reads canonical.** Within the reconciler, derive Framed-IP from the active
   `IPAssignment` row (column → cache) and resolve desired group/state from `derive_access_state`.
   Add `provisioning_state` only if step 1 shows mid-flight-suspend is a real occurrence.

## 5b. Root cause + measured drift (2026-06-17)

**The root cause, stated precisely:** connectivity state is *derived from three denormalized
caches*, not *projected from one set*. RADIUS builds its reply from
`subscriptions.ipv4_address` (a cache) for v4, from a pool on the RADIUS profile for v6, and from
`subscriber_additional_routes` for extra blocks — three sources, no single owner, many independent
writers (provisioning, legacy BSS sync, ONT ops, admin endpoints, the RADIUS rebuild) that race. Every
symptom we chased (de-IP on resume → #282; IPv6-only after inventory return; silent desync) is one
disease: an assignment can be left in a state that contradicts another because nothing treats the
assignment *set* as the single truth.

**The end-state — "treat all IP assignments the same":**

1. **RADIUS reply is derived purely from the active `IPAssignment` set.** Every active assignment
   (v4, v6, routed blocks) projects to its attribute (Framed-IP / delegated-prefix / Framed-Route)
   by *one* rule. `subscriptions.ipv4_address` is dropped as a source (kept at most as a read cache).
   No per-version, no per-`allocation_type` special-casing.
2. **Lifecycle acts on the subscriber's assignment set uniformly.** Release/reactivate handle all
   versions and allocation-types together, so v4 and v6 cannot drift apart (today
   `_release_wan_static_ip_for_inventory_return` releases only `allocation_type="wan"` v4, leaving
   v6 alive → IPv6-only).
3. **One reconciler is the sole writer of that projection.** Everything else just mutates
   assignments; the reconciler re-projects.

**Wrinkle to resolve in the projection rule:** routed blocks live in `SubscriberAdditionalRoute`,
*not* `IPAssignment`, on purpose (a routed /29 has no host-address row, and the `ip_assignments`
check constraint requires one — see the model comment at `network.py:558`). So "the active
assignment set" is the union of `IPAssignment` rows **and** `SubscriberAdditionalRoute` rows; the
one projection rule must read both, or the two tables must be unified behind one view.

**Measured drift (step-1 audit, full prod, 2026-06-17):**

| metric | count | of population |
| --- | --- | --- |
| population (active subs expected to carry a pinned IPv4) | 4022 | — |
| `assignment_mismatch` (column ≠ IPAM) | 323 | 8.0% |
| `assignment_missing` (column set, no active IPAM row) | 270 | 6.7% |
| `radreply_mismatch` / `radreply_missing` / `radreply_orphan` | **0 / 0 / 0** | 0% |
| **total drift** | **593** | **14.7%** |

**The critical reading — it inverts the convergence direction.** `radreply` matches the column for
*every* sub (it is *built from* the column), so the column/radreply side is internally consistent
and **is what customers are actually using on the BNG right now**. The IPAM `IPAssignment` set — the
side the end-state wants to make truth — is the **drifted, neglected** side for 593 active subs.

Therefore: **had we flipped the step-2 reconciler to `apply=True` with "IPAM is truth", we would
have rewritten the served Framed-IP for 323 live customers to whatever the stale ledger says — a
mass IP change / outage.** This is the audit-gauge-first plan paying for itself: the safe sequence
is the *inverse* of the naive one.

**Step-2a repair plan (dry-run vs prod, 2026-06-17)** — `ip_assignment_repair.py` /
`scripts/one_off/repair_ipam_to_served.py`. Subscriber-grained (3978 subs): 3667 already correct;
**310 safely actionable** (1 `backfill_create`, 4 `repoint`, **305 `reclaim_stale`**); **1 conflict**
(`conflict_ambiguous_multi_active`). The 305 reclaims are the asymmetric-release fingerprint: the
served IP is held in IPAM by another subscriber who is provably **not** served it (194 served a
different IP, 111 have no active service) — **0 live contention**, 0 served IPs shared. Repair is
ledger-only (repoint the single address row to the real served owner); it never changes a served IP.

**Grain note (answers "IP on subscriber or service?"):** the served IP is a column on the
*subscription* (service); `IPAssignment` is *modeled* per-subscription (`subscription_id`) but
**operates per-subscriber** because (a) prod never got the `subscription_id` column (mig 153 wedge)
and (b) RADIUS replies one Framed-IP per credential, and credentials are per-subscriber. So the
effective grain is "the active subscription's IP, per subscriber-login". This collapses for the
3977 single-service subscribers; the 1 `conflict_ambiguous_multi_active` is the only sub with two
active services claiming different IPs — the one case where subscriber-grain is genuinely lossy, so
it's refused for human review rather than guessed.

**Corrected sequence:**

- **2a (now): repair IPAM to match what is actually served.** Backfill the 270 missing assignments
  and correct the 323 mismatched ones from the column/radreply (the live-correct value). This
  changes the *ledger*, not the served IP — non-customer-impacting. Do it in dry-run-first batches.
- **2b (after IPAM is trustworthy): switch the projection** so RADIUS derives v4/v6/routes from the
  assignment set and drop the column as a source. Only safe once 2a brings drift to ~0.

The shadow reconciler shipped in §5 must NOT be flipped to `apply=True` against the mismatch set
until 2a completes — its `set_column` direction (column ← IPAM) is exactly the dangerous one given
this data. It is guarded (`trust_ipam=False` by default) so `apply=True` alone cannot mass-rewrite
served IPs.

## 6. Verified-clean / out of scope

- IPv4 **does** survive a normal suspend → reactivate cycle today (the prompting symptom is not
  reproducible on the current path).
- Add-ons / data top-ups survive block/reactivate (time-windowed, status-independent).
- The reject-pool IP-swap is disabled; only the recovery branch remains live.
- **Block-path trace — DONE (2026-06-17).** The real payment-suspension path is the single-writer
  sweep `_external_sync_users` (`radius.py:1201-1225`), reached via
  `collections/_core → suspend_subscription → subscription_suspended → _enforce_subscription_block`.
  It is destructive to `radreply` (confirms R2). The two named block helpers
  (`block_external_radius_credentials`, `cleanup_subscription_on_suspend`) are NOT on this path.
  The reconciler (step 2) must therefore subsume the sweep's status-aware behaviour, not bypass it.
