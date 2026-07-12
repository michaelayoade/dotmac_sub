# Settings Control Plane

## Ownership

Configuration has four explicit owners:

1. Process configuration is environment-owned. This includes database, Redis,
   OpenBao, worker topology, ports, and other values required before a database
   session can exist.
2. Runtime controls are `DomainSetting` rows registered in
   `app.services.settings_spec`. The active database row is authoritative.
3. Secret values live in OpenBao. A secret `DomainSetting` stores only a
   `bao://` reference and its registry specification sets `is_secret=True`.
4. Deployment safety gates may remain environment-owned, but they must not also
   have an independent runtime reader. Explicit exceptions are enforced by the
   settings architecture tests.

Runtime resolution order is:

1. Active database row
2. Registered bootstrap environment variable
3. Registered default
4. Unset

Environment variables initialize missing rows; they do not override a later
operator change. `scripts/utils/settings_sync.py` is the explicit operation for
applying environment values to existing database rows.

## Lifecycle

- `SettingSpec` owns type, default, bounds, allowed values, environment name,
  label, and secret classification.
- `seed_registered_settings()` inserts all missing registered settings in one
  transaction. Startup no longer maintains one seed function per domain.
- Admin form updates use `DomainSettings.upsert_many_by_key()` so a domain form
  is committed atomically.
- API writes use the same registry normalizer as the admin settings surface.
- Inactive settings are treated as absent and fall through to the bootstrap
  environment or default.
- `SettingsCache` is invalidated only by domain services; consumers never write
  it directly.

## Dynamic Records

Some existing operational records still use the settings table but are isolated
behind domain services:

- notification SMTP sender and event-channel keys
- import/export templates and export-job state
- RADIUS reject runtime state and device-login sync status

They are recognized by `settings_health.inspect_settings()` and are not shown as
unknown controls. New dynamic record families must use a dedicated model rather
than extending this list.

## Verification

`tests/architecture/test_settings_control_plane.py` prevents:

- duplicate environment ownership
- a setting domain without a service owner
- raw `DomainSetting` queries or constructors outside control-plane services
- duplicate direct environment readers for registered runtime settings

`settings_health.inspect_settings()` reports unknown active rows, malformed
registered values, inactive registered rows, and secret-classification or
OpenBao-reference mismatches. The result is included in the admin system health
context.

Registered controls with no runtime reader are excluded from the active
registry. After deployment, review and then soft-deactivate their historical
rows with:

```bash
python -m scripts.utils.settings_cleanup
python -m scripts.utils.settings_cleanup --apply
```
