# Phase 2 — `access_state` Column + Pure Helper

**Status**: ready to execute
**Owner**: TBD
**Last updated**: 2026-05-26
**Prerequisites**: phase 1 (groups provisioned in RADIUS + NAS pools) —
the column is independent of NAS work, but phase 3 needs both
**Risk**: low — nullable column + pure function; no behavior change

## Goal

Add the `subscription.access_state` column that phases 3+ will populate
and read. Add the pure mapping function `derive_access_state` that
turns `SubscriptionStatus + captive_redirect_enabled` into an
`AccessState`. Neither change is wired into the event handlers or the
sync paths yet.

## What changes

| Layer | Change | Risk |
|---|---|---|
| `app/models/catalog.py` | New `AccessState` enum; new nullable `Subscription.access_state` mapped column | Zero — column is nullable, no constraint |
| `app/services/radius_access_state.py` | New module with `derive_access_state(status, captive_redirect_enabled)` pure function | Zero — not called from anywhere yet |
| Alembic | New migration `114_add_subscription_access_state` adds nullable `VARCHAR(20)` column | Low — idempotent, additive |
| `tests/test_radius_access_state.py` | Pure-function tests covering all SubscriptionStatus values | Zero — test-only |

## Why VARCHAR(20) and not a PG enum

The latest Subscription column additions (e.g. `113_add_catalog_offer_plan_family`)
use `sa.String` not `sa.Enum`. PG enum types are painful to extend
(`ALTER TYPE ... ADD VALUE` is non-transactional). Strings let us add
new states (`throttled`, `trial_expired`) with code-only changes. The
Python `AccessState` enum still gates writes via the helper functions
in phase 3+.

## Steps

### 1. Code review

Already merged via this PR:
- `app/models/catalog.py:135-156` — `AccessState` enum + docstring
- `app/models/catalog.py:723-725` — column mapping
- `app/services/radius_access_state.py` — helper module
- `alembic/versions/114_add_subscription_access_state.py` — migration

### 2. Apply the migration in staging

```bash
docker exec dotmac_sub_app make migrate
# Or directly:
docker exec dotmac_sub_app alembic upgrade head
```

Verify:

```bash
docker exec dotmac_sub_app python -c "
from app.db import SessionLocal
from sqlalchemy import text
db = SessionLocal()
cols = db.execute(text(\"SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_name='subscriptions' AND column_name='access_state'\")).all()
print(cols)
db.close()
"
# Expected: [('access_state', 'character varying', 'YES')]
```

### 3. Spot-check the helper

```bash
docker exec dotmac_sub_app python -c "
from app.models.catalog import SubscriptionStatus, AccessState
from app.services.radius_access_state import derive_access_state

# Smoke test the whole matrix
cases = [
    (SubscriptionStatus.active, False, AccessState.active),
    (SubscriptionStatus.suspended, False, AccessState.suspended),
    (SubscriptionStatus.suspended, True, AccessState.captive),
    (SubscriptionStatus.canceled, False, AccessState.terminated),
    (SubscriptionStatus.pending, False, None),
]
for status, captive, expected in cases:
    actual = derive_access_state(status, captive_redirect_enabled=captive)
    assert actual == expected, f'{status} + captive={captive} → {actual!r}, expected {expected!r}'
    print(f'  ok: {status.value:<10} captive={captive} → {actual!r}')
print('all good')
"
```

### 4. Run tests

```bash
poetry run pytest tests/test_radius_access_state.py -v
# Expected: 11 passed
```

## Rollback

```bash
docker exec dotmac_sub_app alembic downgrade -1
```

The model code (`AccessState` enum + mapped column) can stay even after
downgrade — SQLAlchemy will just see `None` for the missing column at
load time, which is fine because nothing reads it yet. If you want a
true revert, also revert the `app/models/catalog.py` and
`app/services/radius_access_state.py` changes.

## Exit criteria (must all be true to move to phase 3)

- [ ] Migration applied in staging; column visible in
  `information_schema.columns`
- [ ] `derive_access_state` smoke test prints "all good"
- [ ] `pytest tests/test_radius_access_state.py` — 11/11 pass
- [ ] Production migration applied (same `alembic upgrade head`)
- [ ] No customer-visible behavior change in 24h post-deploy

## What phase 3 will add

- A new `set_subscription_access_state(db, sub_id, state)` function in
  `app/services/radius_access_state.py` that:
  1. UPDATES `subscription.access_state` in the app DB
  2. UPSERTs `radusergroup` membership in the external RADIUS DB
  3. Optionally CoA-disconnects live sessions for suspend/terminate
- A call in the existing event handler that computes the derived state
  and calls `set_subscription_access_state` _in addition to_ the
  existing block path (shadow write — old path stays authoritative).
- A feature flag (`DomainSetting` — `radius_group_routing_enabled`)
  that gates whether the shadow write runs.
