# Phase 5 — Bounded-Batch Backfill

**Status**: in progress (canary applied; full run executing)
**Owner**: TBD
**Last updated**: 2026-05-26
**Prerequisites**: phases 1-4 complete
**Risk**: low — idempotent, resumable, per-row commits, shadow path
only (legacy block still authoritative)

## Goal

Populate `subscription.access_state` + the external RADIUS
`radusergroup` mirror for every existing subscription, so that
phase 7 has a complete dataset to switch FreeRADIUS over to
group-based lookups.

## What it does

For each subscription:

1. Read `subscription.status` + `subscriber.captive_redirect_enabled`
2. Compute target state via `derive_access_state` (phase 2 helper)
3. Call `set_subscription_access_state` (phase 3 dual-write)
   - app DB: UPDATE `subscriptions.access_state`
   - external RADIUS: UPSERT/DELETE `radusergroup` row
4. Commit
5. On exception: log + rollback + record in errors list, keep going

| State outcome | radusergroup write |
|---|---|
| `active` | INSERT row `groupname='dotmac-active'` |
| `suspended` | INSERT row `groupname='dotmac-suspended'` |
| `captive` | INSERT row `groupname='dotmac-captive'` |
| `terminated` | DELETE any `dotmac-*` rows |
| `None` (pending/hidden/archived) | DELETE any `dotmac-*` rows |

## Script

`scripts/migration/phase5_backfill_access_state.py`

Key flags:

| Flag | Default | Purpose |
|---|---|---|
| `--dry-run` | off | print intended writes, no DB changes |
| `--limit N` | none | cap rows per invocation (canary mode) |
| `--batch-size N` | 200 | DB page size for the select cursor |
| `--sleep-between-batches S` | 0.0 | pause between DB pages, for backpressure |
| `--log-every N` | 100 | log progress checkpoint every N rows |
| `--include-migrated` | off | also re-process subs whose `access_state` is already set (full re-sync) |

Resumability: with `--include-migrated` off (default), the script
filters `WHERE access_state IS NULL`. Each row's commit removes it
from the next iteration's query result. Interrupting and re-running
naturally picks up where it left off.

## Canary run (2026-05-26)

```
$ docker exec dotmac_sub_app sh -c \
    "PYTHONPATH=/app python scripts/migration/phase5_backfill_access_state.py --limit 10"
phase5 backfill starting — dry_run=False limit=10 batch_size=200 include_migrated=False
--limit reached, stopping
=== Summary ===
Total processed: 10
  active       : 5
  suspended    : 1
  terminated   : 4
External radusergroup rows written: 6
Errors: 0
```

Cross-store verification immediately after:

```
app DB:    {None: 12309, 'active': 5, 'suspended': 1, 'terminated': 4}
external:  dotmac-active: 5, dotmac-suspended: 1   (terminated → no row, by design)
```

State counts match exactly across both stores. Canary passed.

## Full backfill (in progress)

Same command without `--limit`. Approx 12309 remaining rows at ~30
subs/sec steady-state ≈ 7 minutes wall time. No customer auth impact
because the shadow path is still gated by the feature flag (default
OFF) for the event handler — the backfill writes to the dormant
infrastructure that nothing reads yet.

## Rollback

```sql
-- App DB: reset all access_state to NULL.
UPDATE subscriptions SET access_state = NULL;

-- External RADIUS: wipe all dotmac-* group memberships.
DELETE FROM radusergroup WHERE groupname LIKE 'dotmac-%';
```

Both commands are safe to run because (a) nothing currently reads
`access_state`, and (b) `radusergroup` is not yet wired into the
FreeRADIUS auth path that gates real customer traffic (phase 7 will
flip that).

## Exit criteria

- [x] Canary on 10 rows succeeds with zero errors, cross-store
  consistent
- [ ] Full backfill completes with error rate < 1%
- [ ] Post-backfill: `SELECT COUNT(*) FROM subscriptions WHERE access_state IS NULL`
  returns 0
- [ ] Post-backfill: `SELECT COUNT(DISTINCT username) FROM radusergroup
  WHERE groupname LIKE 'dotmac-%'` matches the count of active +
  suspended + captive subscriptions (terminated correctly excluded)
- [ ] No customer-visible behavior change in 24h post-deploy (shadow
  writes only; legacy block path still authoritative)

## What phase 6 adds

Phase 6 — verification — runs cross-store consistency checks at scale:

- For 100 sampled customers, confirm that `derive_access_state(sub)`
  agrees with their `subscription.access_state` and `radusergroup`
  row
- Set up a periodic divergence check (cron + log) that runs the same
  comparison continuously
- Define the SLO for divergence (e.g., 0% within 5 minutes of any
  state change, since the shadow write is synchronous in the event
  handler)

Once phase 6 passes, phase 7 is safe to schedule.
