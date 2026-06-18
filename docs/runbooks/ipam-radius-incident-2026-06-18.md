# Runbook — IPAM/RADIUS provisioning incident (2026-06-18)

Rollout for the two incident PRs. Three distinct faults (see commit messages):

- **F1** — pools with no materialized `ipv4_addresses` rows can't assign IPs → no
  Framed-IP → customer offline. Fix: **PR #294** (on-demand allocator).
- **F2** — `Subscriber.status == blocked` walls a customer regardless of an active
  subscription; **1,652** unambiguously drifted. Fix: **PR #296** reconciler.
- **F3** — subscriber `c8739bc8`'s routed `/30` is an inactive
  `SubscriberAdditionalRoute` (no `Framed-Route`). Fix: **PR #296** one-off.

> Nothing here is automatic. Every step is dry-run-first; apply only after the
> verification for the prior step passes.

## Ordering (important)

1. **Deploy #294 first.** It adds the active-`SubscriberAdditionalRoute`
   exclusion to the allocator. The c8739bc8 repair (step 3) reactivates a routed
   `/30`; once that route is active the allocator must already know those hosts
   are unavailable, or it could hand a routed host to another subscriber.
2. Re-provision the 20 F1 subs.
3. c8739bc8 routed-`/30` repair — **only after #294 is live.**
4. #296 subscriber-status drift reconcile.

## Step 1 — deploy #294 (allocator)

Merge #294, deploy, restart the app + celery workers (the bind-mount needs a
worker reload to pick up `provisioning_helpers.py`). No data change on its own.

## Step 2 — re-provision the 20 F1 subscribers

#294 fixes *"no materialized rows"*, **not** *"wrong pool selected"* — pool
resolution still falls back to the first active pool alphabetically when NAS→pool
is ambiguous (88 active v4 pools, only 55 NAS-linked). So **do not rely blindly
on pool fallback**:

- For each of the 20, determine the intended pool. If `provisioning_nas_device_id`
  resolves to exactly one NAS-linked pool, that's deterministic. Otherwise pass
  **explicit pool context** (`ipv4_pool_id`) or verify the resolved pool before
  applying.
- Then trigger provisioning (re-emit `subscription_activated`, or invoke
  `ensure_ip_assignments_for_subscription` with the verified context) → refresh
  RADIUS → CoA-kick the session.

Cohort query (read-only):

```sql
SELECT s.id, s.login, s.provisioning_nas_device_id
FROM subscriptions s
WHERE s.status='active'
  AND (s.ipv4_address IS NULL OR s.ipv4_address='' OR s.ipv4_address='0.0.0.0')
  AND NOT EXISTS (SELECT 1 FROM ip_assignments ia
                  WHERE ia.subscriber_id=s.subscriber_id
                    AND ia.is_active=true AND ia.ip_version='ipv4');
```

Verify per sub after apply: `subscriptions.ipv4_address` set, an active v4
`IPAssignment` exists, and `radreply` carries `Framed-IP-Address`.

## Step 3 — c8739bc8 routed-/30 repair (after #294 is live)

```
docker compose exec -T -e PYTHONPATH=/app app \
  python scripts/one_off/repair_routed_block_c8739bc8.py            # dry-run
```

Dry-run MUST assert all invariants before any apply (the script aborts otherwise):
exact subscriber `c8739bc8…` / login `100025880`; `160.119.126.160/30` route
exists and is **inactive**; no other **active** route overlaps it; the one
intended primary (`160.119.126.18`) is untouched; `.161/.162` carry only
**inactive** assignments owned by this subscriber (no active-assignment conflict
inside the routed block).

```
docker compose exec -T -e PYTHONPATH=/app app \
  python scripts/one_off/repair_routed_block_c8739bc8.py --apply
```

Verify: `radreply` for `100025880` now has `Framed-Route 160.119.126.160/30 …`
alongside the unchanged `Framed-IP-Address 160.119.126.18`.

## Step 4 — #296 subscriber-status drift reconcile

```
docker compose exec -T -e PYTHONPATH=/app app \
  python scripts/one_off/reconcile_blocked_subscriber_drift.py          # dry-run
```

Before/after checks:

1. **Dry-run `candidates` should equal 1,652** (the verified cohort) — or explain
   the delta before proceeding (numbers move as customers transact).
2. Sample apply:
   ```
   ... reconcile_blocked_subscriber_drift.py --apply --limit 25
   ```
   Then verify for those 25: `subscribers.status` flipped to `active`; their
   logins' `radreply` no longer has `Mikrotik-Address-List=suspended`; CoA /
   session behaviour is correct (session re-auths un-walled).
3. **Full apply only after the sample passes.**
4. The **1,095 mixed-status** accounts stay excluded — the cohort finder requires
   *all* subscriptions active, so they are never selected. Do not force them.

The reconciler refresh path is injectable; its default prefers
`app.services.radius_population.populate` and falls back to the committed
`scripts.migration.populate_radius_from_subs.populate`, so it works on `main`
before the Splynx-decommission relocation lands.

## Out of scope / follow-ups

- **Resume doesn't reactivate `SubscriberAdditionalRoute`** — routed-block
  customers lose extra space on suspend→resume. Needs entitlement/provenance; the
  c8739bc8 one-off covers only the known case.
- **Pool-resolution determinism** — back the alphabetical fallback with NAS links.
- **RADIUS sweep is untracked working-tree code** — relocation belongs to the
  Splynx-decommission refactor; snapshot captured at
  `/root/radius-sweep-snapshot-2026-06-18/` (move to a real artifact store).
