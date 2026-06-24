# Connectivity State Machine — implementation spec

Date: 2026-06-19 · Status: **spec / not started**
Parent: [SERVICE_LIFECYCLE_BUNDLE_INTEGRITY.md](./SERVICE_LIFECYCLE_BUNDLE_INTEGRITY.md) (the "why" + measured drift)
Related: `docs/radius_state_refactor/phase0_state_model.md` (the RADIUS-group plumbing this sits above)

## 0. Why this doc

The parent doc decided the rule — *"status transitions mutate desired state only; one
idempotent reconciler is the sole writer of connectivity state."* It built the audit gauge
(step 1), repaired IPAM-vs-served drift (step 2a, 593→1), and shipped an **IPv4-only,
shadow, unwired** seed: `app/services/connectivity_reconciler.py`.

What does **not** yet exist, and what this spec defines so it's implementation-ready:

1. The **full transition table** — desired connectivity state for *every* lifecycle state,
   across *all* dimensions (IP set, internal RADIUS/credential flags, access_state, external
   projection, live session), not just the IPv4 column.
2. The **exact callsite migration list** — every one of the ~35 verified direct writers,
   classified keep / absorb / convert / delete / flag.
3. The reconciler's **locked-transaction + idempotent side-effect contract**.
4. The **guardrails and incident-shaped tests** to land *before* migrating callers.

This is the orchestration layer **above** the radius_state_refactor: that refactor owns
`access_state → radusergroup`; this reconciler owns deriving access_state *and* the IP /
credential dimensions from one source-of-truth read, and is the only thing that calls the
projection.

## 1. The model: one source of truth, many derived caches

**Source of truth (read under lock, never written by the reconciler):**

| Input | Where | Grain |
| --- | --- | --- |
| `subscription.status` | `catalog.py:769` | subscription |
| `subscriber.captive_redirect_enabled` + `hard_reject` (fraud) | subscriber / lock reason | subscriber |
| active `EnforcementLock` rows | `enforcement_lock.py` | subscription+reason (governs *restore eligibility*, not desired connectivity directly — status already reflects them) |
| the active **assignment set**: `IPAssignment` (v4/v6) ∪ `SubscriberAdditionalRoute` (routed blocks) | `network.py:498` / additional-routes | subscriber |

**Derived outputs (the reconciler is the sole writer of all of these):**

| Output | Field/target | Today's writer count |
| --- | --- | --- |
| Access state | `subscription.access_state` + `radusergroup` (via `set_subscription_access_state`) | 1 (already centralized) |
| Internal RADIUS user flag | `RadiusUser.is_active` | 6 |
| Credential flag | `AccessCredential.is_active` | 7 |
| IP activation | `IPAssignment.is_active` | 12 |
| Served-IP cache | `subscription.ipv4_address` / `ipv6_address` | 11 / 3 |
| External RADIUS | `radcheck`/`radreply`/`radusergroup` | 1 (`_external_sync_users` sweep — already single-writer, reconciler *enqueues* it) |
| Live session | CoA-disconnect / SSH kick | `disconnect_subscription_sessions` (keep) |

The reconciler computes the desired value of each output from the source of truth and
converges. Everything else may only mutate the **source** (status, the assignment set).

> **Grain (settled in parent §5b):** effective grain is "the active subscription's IP, per
> subscriber-login" — IPAssignment is subscriber-scoped in prod (mig-153 wedge) and RADIUS
> replies one Framed-IP per credential. The one genuinely multi-active-service subscriber is
> refused for human review, not guessed.

## 2. Transition table (desired connectivity state)

`derive_access_state` (`radius_access_state.py:83`) already maps status→AccessState. The
reconciler extends that single derivation to every dimension:

| Lifecycle state | access_state | Cred / RadiusUser.is_active | IP assignment set | `ipv4_address` cache | external RADIUS rows | live session |
| --- | --- | --- | --- | --- | --- | --- |
| `pending` / `hidden` / `archived` | none (no row) | inactive (or absent) | none | null | none | n/a |
| `active` | `active` | **active** | **active**, pinned | = served (IPAM after 2b) | full radcheck+radreply, `dotmac-active` | leave running |
| `suspended`/`blocked`/`stopped` (default) | `captive` | **active** (reversible) | **retained active** | **retained** | `dotmac-captive` (+ walled-garden attrs) | CoA-disconnect once |
| `suspended` + `hard_reject` (fraud) | `suspended` | active | retained | retained | `Auth-Type := Reject` only | CoA-disconnect once |
| `canceled`/`expired`/`disabled` | `terminated` | **inactive** | **released** | **null** | none (user-not-found) | CoA-disconnect once |

**Load-bearing invariants encoded here (each is a past incident):**

- **INV-1 — IP retained across suspend.** Only *terminal* releases the assignment / nulls the
  cache. Suspend keeps the address so restore returns the same IP. (Encodes #282 paid→offline;
  today only `cleanup_subscription_on_cancel` nulls the column — the reconciler must preserve
  that and forbid any suspend-path release.)
- **INV-2 — all versions move together.** Release/reactivate act on the whole assignment set
  (v4 + v6 + routes), never one `allocation_type`. (Encodes the IPv6-only-after-ONT-return bug:
  `_release_wan_static_ip_for_inventory_return` releases only `wan` v4 today.)
- **INV-3 — credential survives suspend, dies on cancel.** `is_active` flips False only on
  terminal. (Matches current suspend=reversible / cancel=destructive split.)
- **INV-4 — the cache never becomes the only copy.** `ipv4_address` is a projection of the
  active `IPAssignment`; the reconciler rebuilds it, so a stray write self-heals on next run.
  (Encodes R2.)
- **INV-5 — CoA is idempotent.** Kick only when state≠active *and* a live session exists;
  re-running finds none. No "kick once" bookkeeping needed.

**Pure function shape** (extends the existing `derive_access_state`):

```python
# app/services/connectivity_reconciler.py
@dataclass(frozen=True)
class DesiredConnectivity:
    access_state: AccessState | None      # None = no radusergroup row
    credentials_active: bool              # AccessCredential.is_active + RadiusUser.is_active
    ip_active: bool                       # assignment set should be active
    ip_retained: bool                     # keep the address vs release it
    kick_live_session: bool               # CoA if a session is up

def derive_desired_connectivity(
    status: SubscriptionStatus, *, hard_reject: bool = False
) -> DesiredConnectivity: ...
```

This is the **single** place the state table lives. `set_subscription_access_state`,
the IP release/ensure helpers, and the credential flags all become *appliers* of this struct,
not independent decision-makers.

## 3. Callsite migration list

Verified inventory (file:line confirmed this session). Disposition codes:
**ABSORB** = becomes a private primitive the reconciler calls (no other caller); **CONVERT**
= caller stops writing connectivity, instead mutates source state / requests a reconcile;
**KEEP** = legitimately independent (allocation primitive, gated one-off, or self-service),
but must call the reconciler after if it changes derived state; **DELETE** = dead/redundant;
**FLAG** = keep behind a metric+feature-flag during migration.

### IPAssignment.is_active (12)
| # | Site | Dir | Disposition |
| --- | --- | --- | --- |
| 1-2 | `ip_lifecycle.release_service_ips_for_subscription:105,116` | →F terminal | **ABSORB** — reconciler's release primitive (extend to v6+routes per INV-2) |
| 3-4 | `ip_lifecycle.apply_backlog_cleanup:295` / `reactivate_assignments:312` | ↔ | **KEEP** — gated CLI repair/rollback |
| 5 | `network/ip.py IPAssignments.delete:237` | →F | **KEEP+reconcile** — admin API; call reconciler after |
| 6 | `ont_inventory._release_wan_static_ip_for_inventory_return:620` | →F | **CONVERT** — violates INV-2 (v4-only); route through reconciler release |
| 7 | `subscriber_wan_ipam.ensure_wan_static_ip_available:156` | →T | **KEEP** — allocation primitive (allocation ≠ activation policy) |
| 8-9 | `ip_assignment_repair:282,293` | ↔ | **KEEP** — step-2a repair reconciler (fold in later) |
| 10 | `provisioning_helpers._ensure_ip_assignment_for_version:397` | →T | **ABSORB** — the "ensure desired IP active" primitive |
| 11-12 | `web_system_restore_tool:217,421` | ↔ | **FLAG** — admin restore; route through reconciler |

### subscription.ipv4_address / ipv6_address (11 / 3)
| # | Site | Disposition |
| --- | --- | --- |
| 1-2 | `network/ip.py _sync_subscription_ipv4:106,111` | **ABSORB** — this *is* the projection helper |
| 3 | `network/ip.py IPAssignments.update:223` | **KEEP+reconcile** (admin) |
| 4 | `ip_lifecycle.release_service_ips_for_subscription:121/123` | **ABSORB** |
| 5 | `enforcement.cleanup_subscription_on_cancel:1067/1068` | **ABSORB** — terminal path |
| 6 | `connectivity_reconciler.apply_connectivity_actions:196` | **KEEP** — this is the reconciler |
| 7 | `radius_reject.enforce_subscription_reject_ip:317` (assign reject IP) | **DELETE** — dead (`:265` early-returns `noop_block_swap_disabled`) |
| 8 | `radius_reject:241` (restore original IP) | **ABSORB** — recovery branch folds into reconciler |
| 9 | `usage._write_subscription_ips_from_accounting:182,184` | **CONVERT (priority)** — writes the *observed* live IP into the *desired* column → fights the reconciler. Move to a separate `observed_ipv4`/last-seen field. **See §3.1.** |
| 10 | `web_catalog_subscriptions.update_subscription_with_audit:3557` | **KEEP+reconcile** (admin edit) |
| 11 | `web_provisioning_bulk_activate.execute_job:446` | **KEEP+reconcile** (bulk) |

### RadiusUser.is_active (6) / AccessCredential.is_active (7)
| Site | Disposition |
| --- | --- |
| `enforcement.cleanup_subscription_on_cancel:1050,1062` / `cleanup_subscription_on_suspend:1163` / `restore_subscription_connectivity:1231` | **ABSORB** — credential/RADIUS flag is reconciler-owned |
| `radius.ensure_radius_users_for_subscription:1013,1028` / `_ensure_access_credential:591,610` / `web_catalog._ensure_access_credential:1016,1020` / `web_provisioning_bulk_activate._upsert_access_credential:372,376` | **KEEP** — provisioning *creates* the row; reconciler owns the *active flag* policy |
| `radius._external_radius_sync_run:1570` | **KEEP** — external mirror (read-only-from-RADIUS by refactor phase 10) |
| `auth.delete_user_credential:200` | **KEEP** — user self-service revoke (legit independent) |
| `web_system_restore_tool:201,209,405,413` | **FLAG** — admin restore |

### access_state (1)
`set_subscription_access_state:211` — **KEEP** as the reconciler's RADIUS-group applier (the
reconciler is its only caller after cutover; satisfies refactor invariant #4).

### §3.1 The one new finding: the accounting writer
`usage._write_subscription_ips_from_accounting` mirrors the **live framed IP from accounting**
into `subscription.ipv4_address`. That column is *also* the desired-IP projection target. So
the observed and desired values share one column and overwrite each other — exactly the
"derived from caches, not projected from one set" disease. **Fix: split the field** — observed
live IP → a new nullable `last_seen_framed_ipv4` (display only); desired/served IP stays
reconciler-owned. Low-risk, unblocks making the column authoritative in step 2b.

## 4. The reconciler as sole writer

One entry point, growing `converge_subscription_connectivity` from IPv4-only to all dimensions:

```python
def converge_subscriber_connectivity(
    db, subscriber_id, *, apply=False, trust_ipam=False, reason="transition"
) -> ConvergePlan:
```

**Transaction boundary (reuse existing primitives — nothing new invented):**

1. `lock_for_update(db, Subscriber, subscriber_id)` + `lock_multiple(db, Subscription, sub_ids)`
   (`locking.py:55,113` — sorted, deadlock-safe; matches `account_lifecycle.py:136`).
2. **Re-read** status, locks, and the assignment set *inside* the lock (never trust the
   pre-lock snapshot — this is what kills the dunning-vs-payment class of race).
3. `derive_desired_connectivity(...)` — pure, no I/O.
4. Apply **local DB** changes (IPAssignment.is_active, credential flags, access_state column,
   ipv4 cache projection) in this one transaction. Commit.
5. **External side-effects go through the outbox / idempotent task, not inline:**
   - external RADIUS: `refresh_radius_from_subs.delay()` (already the single writer).
   - NAS SSH / address-list / CoA: enqueue as a Celery task wrapped in `@idempotent_task`
     (`task_idempotency.py:178`, key = `conn:{subscriber_id}:{desired_hash}`), **not** the
     current inline try/except chain in `ProvisioningHandler`.

This directly removes the R1 failure mode: today's handler runs allocate→RADIUS→SSH→orders as
four independently `try/except`ed inline steps, so a step-3 failure leaves 1–2 applied with no
retry. Under this design, local state commits atomically and external effects are
retried-to-success by the existing `retry_failed_events` / idempotent-task machinery.

**Wiring (replaces inline orchestration):**
- `events/handlers/provisioning.py` `_handle_subscription_activated/_resumed` → become a single
  `converge_subscriber_connectivity(apply=True)` call.
- `events/handlers/enforcement.py` `_handle_subscription_block/_cancel/_restore` → same.
- The 6h audit (`audit_ip_consistency`) and a new periodic sweep call it with `apply=True` for
  auto-repair — same code path on transition and on audit (the parent rule).

**Idempotency:** every apply re-derives and converges to the same target, so duplicate events,
retries, and the periodic sweep are all safe (INV-5 makes even CoA idempotent).

## 5. Guardrails BEFORE migrating callers

Land these first so the migration is observable and reversible:

1. **Legacy-write detector.** A `_record_direct_connectivity_write(field, caller)` counter +
   Prometheus `connectivity_direct_write{field,caller}`; call it from every CONVERT/FLAG site
   so we can watch direct writes go to zero before deleting them. (Mirror of how
   `radius_ip_consistency_drift` was used in step 1.)
2. **Shadow-diff in prod.** Run the grown reconciler `apply=False` on every transition for one
   release; log desired-vs-actual. Cut to `apply=True` per dimension only when the diff is ~0
   (the audit-gauge-first house pattern; `trust_ipam=False` stays until parent step 2b).
3. **Incident-shaped tests** (`tests/test_connectivity_reconciler.py`, currently 7 — extend):
   - **paid customer keeps IP**: active→suspended→active converges to the *same* IPAssignment
     and `ipv4_address` (INV-1, #282).
   - **suspended loses access exactly once**: convergence sets captive group + CoA; a second
     run is a no-op (INV-5).
   - **restore cannot bypass an unresolved lock**: a `fraud` lock present → reconciler refuses
     `active` connectivity even if status was forced (defends the reseller-portal bypass class).
   - **concurrent payment + dunning converge**: two transitions racing on one subscriber, both
     take the lock, final state is deterministic (active wins iff no active lock).
   - **all versions move together**: ONT return releases v4 *and* v6 (INV-2).
   - **terminal releases, cache nulled, user-not-found** (INV-3/INV-4).
4. **DB-level protection — later, optional.** Once direct writes are zero, consider a trigger/
   CHECK that rejects `ipv4_address` writes outside the reconciler, or a partial unique index
   guaranteeing ≤1 active v4 IPAssignment per subscriber. Don't lead with this.

## 6. Sequencing (extends parent §5 step 2)

- **2c — grow + shadow:** extend `derive_desired_connectivity` to all dimensions; reconciler
  computes full plan `apply=False`; ship legacy-write detector + shadow-diff. *No behavior
  change.*
- **2d — wire transitions:** point the two event handlers at the reconciler (`apply=True`),
  dimension by dimension (credentials → access_state → IP cache), watching the diff gauge.
- **2e — absorb/convert/delete writers** per §3, deleting each only after its detector reads 0.
  Split the accounting field (§3.1) here.
- **2f — periodic auto-repair sweep** calls the same entry point; fold `ip_assignment_repair`
  and `account_status_reconcile` in. Parent step 2b (drop the column as a source) can then
  proceed on a trustworthy IPAM.

Each sub-step is independently revertible (flag flip / re-enable the old writer).

## 7. Non-goals (unchanged from parent)

No `Bundle` ORM aggregate; no control-plane; `SubscriptionStatus` enum unchanged; legacy BSS import
paths unchanged (they write status, not connectivity); routed-block unification behind one
projection view is acknowledged (parent §5b wrinkle) but only required at 2b.
