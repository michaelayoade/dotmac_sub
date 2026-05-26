# Phase 3 — Dual-write (Shadow)

**Status**: ready to execute
**Owner**: TBD
**Last updated**: 2026-05-26
**Prerequisites**: phase 1 RADIUS-side complete; phase 2 column migrated
**Risk**: low — gated behind feature flag, default OFF; legacy block path
remains authoritative

## Goal

Wire `set_subscription_access_state` into the existing enforcement
event handler so that every block / cancel / restore event ALSO mirrors
the derived state into `subscription.access_state` + external RADIUS
`radusergroup`. The legacy block path (IP rewrite + per-user
radcheck/radreply + per-customer firewall address-list) continues to be
the actual blocking mechanism — the new path is shadow until phase 7.

## What changes

| Layer | Change | Default behavior |
|---|---|---|
| `app/services/radius_access_state.py` | New `set_subscription_access_state(db, sub_id, state)` function | n/a — not called yet |
| `app/services/events/handlers/enforcement.py` | New `_shadow_write_access_state` method on `EnforcementHandler`; called at the end of `_enforce_subscription_block` and after the reconcile in `_handle_subscription_restore`. Gated by `_setting_bool(SettingDomain.radius, "group_routing_enabled", False)` | OFF — shadow path is dormant until the DomainSetting is flipped |
| `tests/test_radius_set_access_state.py` | 11 tests covering writes, transitions, idempotency, namespace isolation, no-op paths | n/a |

## The feature flag

A single `DomainSetting`:

| domain | key | type | default |
|---|---|---|---|
| `radius` | `group_routing_enabled` | bool | false (absent → false) |

When `false` (the default), `_shadow_write_access_state` returns early
and nothing is written. When `true`, the new path runs in addition to
the existing block sequence.

## Operational sequence

1. **Deploy the code** (this PR). Flag is absent → false → shadow path
   is dormant. Zero behavioral change for any customer.

2. **In staging**: turn the flag on via admin settings or directly:
   ```sql
   INSERT INTO domain_settings
       (domain, key, value_type, value_json, is_active, is_secret)
   VALUES
       ('radius', 'group_routing_enabled', 'boolean', 'true', true, false);
   ```
   (Schema match per existing settings_seed.py patterns.)

3. **Trigger a block** on one staging customer (e.g., suspend then
   restore them via admin UI). Confirm:
   - `subscription.access_state` is populated with the expected value
     (suspended, then active)
   - External RADIUS `radusergroup` has the matching `dotmac-*` row
   - Customer behavior is unchanged (legacy block path still drives the
     actual auth result, address-list, etc.)

4. **Watch for divergence**. Sample a few customers daily; compare
   their derived `access_state` against the `radusergroup` row that
   exists. If they ever disagree, that's a bug to fix before phase 7.

5. **Promote to production** when staging is stable for ~1 week:
   flip the flag in prod via the same DomainSetting insert.

## Verification commands (run in staging)

```bash
# Confirm the feature flag is off by default — shadow write should
# return early and not change subscription.access_state.
docker exec dotmac_sub_app python -c "
from app.db import SessionLocal
from sqlalchemy import select
from app.models.catalog import Subscription
from app.services.events.handlers.enforcement import EnforcementHandler
db = SessionLocal()
sub = db.scalars(select(Subscription).limit(1)).first()
handler = EnforcementHandler()
handler._shadow_write_access_state(db, str(sub.id))
db.refresh(sub)
print(f'After shadow with flag OFF: access_state={sub.access_state!r} (expect None)')
db.close()
"

# Turn the flag ON.
docker exec dotmac_sub_radius_db psql -U radius -d radius -c "..." # n/a — flag is in app DB, not radius DB
# Use the admin UI or:
docker exec dotmac_sub_app python -c "
from app.db import SessionLocal
from app.models.domain_settings import DomainSetting, SettingDomain
from app.models.subscription_engine import SettingValueType
db = SessionLocal()
existing = db.query(DomainSetting).filter(
    DomainSetting.domain == SettingDomain.radius,
    DomainSetting.key == 'group_routing_enabled',
).first()
if existing:
    existing.value_type = SettingValueType.boolean
    existing.value_json = True
    existing.is_active = True
else:
    db.add(DomainSetting(
        domain=SettingDomain.radius,
        key='group_routing_enabled',
        value_type=SettingValueType.boolean,
        value_json=True,
        is_active=True,
        is_secret=False,
    ))
db.commit()
db.close()
print('feature flag ON')
"

# Re-run the shadow write — should now populate access_state.
docker exec dotmac_sub_app python -c "
from app.db import SessionLocal
from sqlalchemy import select
from app.models.catalog import Subscription
from app.services.events.handlers.enforcement import EnforcementHandler
db = SessionLocal()
sub = db.scalars(select(Subscription).limit(1)).first()
handler = EnforcementHandler()
handler._shadow_write_access_state(db, str(sub.id))
db.refresh(sub)
print(f'After shadow with flag ON: access_state={sub.access_state!r}')
db.close()
"
```

## Rollback

Two levels:

1. **Disable the feature flag** — instant, no code change needed:
   ```sql
   UPDATE domain_settings
       SET value_json = false
       WHERE domain = 'radius' AND key = 'group_routing_enabled';
   ```
   Shadow writes stop immediately. Existing `subscription.access_state`
   values and `radusergroup` rows are left in place but are no longer
   updated. Legacy block path is unaffected.

2. **Revert the code** — `git revert <commit>` undoes the event handler
   wiring. The `set_subscription_access_state` helper remains importable
   but is no longer called by event handlers. Existing data unchanged.

Neither rollback affects any current customer's auth — the legacy block
path has been authoritative throughout phases 1-6.

## Exit criteria (must all be true to move to phase 4)

- [ ] Tests pass: `pytest tests/test_radius_set_access_state.py` (11)
- [ ] Code review approved
- [ ] Deployed to staging with flag OFF — no behavioral change observed
- [ ] Flag flipped ON in staging — verification commands above pass
- [ ] Sample 5 staging customers; confirm `access_state` value matches
  what `derive_access_state(sub.status, captive_redirect_enabled=...)`
  returns
- [ ] No exceptions in logs from `_shadow_write_access_state`

## Watch-outs

- **Long-running transactions**: `set_subscription_access_state`
  writes to the app DB then opens a fresh transaction on the external
  RADIUS DB. If the caller has an open app DB transaction, the
  external write commits independently. This is correct (we don't
  want a hung external write to roll back app DB changes) but it
  does mean a successful app-DB write can be followed by a failed
  external write — the function logs the exception and returns
  partial counts so callers see it.

- **No `_setting_bool` caching**: every `_shadow_write_access_state`
  call hits the DB for the flag. Cost is low (one indexed lookup)
  but if it becomes a hot path consider adding to a request-scoped
  cache.

- **Phase 4 will backfill ONE customer** by hand using
  `set_subscription_access_state` directly to exercise the production
  external RADIUS DB and confirm no schema/encoding surprises.

## What phase 4 adds

- Pick one production customer (NOT the test user 100025610 — pick a
  real one with `captive_redirect_enabled` set, so we exercise the
  captive path).
- Manually call `set_subscription_access_state(db, sub_id, derive_access_state(...))`.
- Verify the radusergroup row appears in the production external RADIUS DB.
- Verify the customer's auth behavior is unchanged (legacy path still
  authoritative).
- Document the result. If no surprises, phase 5 backfills all customers
  with a bounded-batch script.
