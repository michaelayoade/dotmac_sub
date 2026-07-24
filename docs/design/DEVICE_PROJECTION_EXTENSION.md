# Device projection extension (option b) — implementation plan

Extends the device projection so **NAS + MikroTik routers** become first-class
device types and per-class operational facts live in the projection, enabling one
unified Device ledger + Device 360 to retire the 8 per-class tables. Companion to
`docs/design/NETWORK_IA_RATIONALIZATION.md`. `device_projections` is a rebuildable
cache (reconciler `network.device_projection` is sole writer), so all of this is
additive and back-fills on the next reconcile pass.

## Slice status
- **b1 DONE** (commit 1fb65f91): `DeviceProjection.class_facts` JSONB (nullable) +
  migration `388_device_projection_class_facts` (off single head `387`). Tests green.
- **b2 / b3 / b4 pending** — below. b2 (reconciler dedup) is the highest-risk piece.

## b2 — reconciler: collect NAS + routers, populate class_facts

`collect_devices` (`app/services/web_network_core_devices_inventory.py:247`) — add two
append-blocks mirroring the OLT block, then add `"class_facts"` to `_PROJECTED_FIELDS`
in `device_projection_reconcile.py` (straight copy of the dict).

**Sources**
- NAS: `NasDevices.list(db, is_active=True)` (`app/services/nas/devices.py`); model
  `NasDevice` (`catalog.py:937`). Map: `management_ip`→ip_address, `vendor`(enum
  `.value`)/`model`/`serial_number`, `firmware_version`. Has `pop_site_id` (FK) → site.
- Router: `RouterInventory.list(db)` (`app/services/router_management/inventory.py`);
  model `Router` (`router_management.py:103`). Map: `management_ip`/`hostname`→ip_address,
  `board_name`→model, `routeros_version`; `location` is free-text (no site FK).

**Dedup — HIGHEST RISK (recon constraint #1).** One physical MikroTik can be a
`Router` **and** a `NasDevice` **and** a `NetworkDevice` at once (via `Router.nas_device_id`,
`Router.network_device_id`, `NasDevice.network_device_id`). The existing core-vs-olt
dedup uses a `seen_keys` set on mgmt_ip/hostname/name — extend it, but prefer
**FK-based dedup** (`network_device_id` / `nas_device_id`) which is precise. Order the
collection so the most authoritative representation wins (e.g. skip a Router row whose
`network_device_id` already emitted a core/nas row, per an explicit precedence you
document). Without this the ledger lists the same box 2–3×.

**Derivers** (`app/services/device_operational_status.py`) — add
`derive_nas_operational_status` and `derive_router_operational_status`, both emitting
the binary `DeviceOperationalState` (`working`/`not_working`):
- Router: `RouterStatus` online|degraded→working,
  offline|unreachable|maintenance→not_working; last-seen age makes verification
  due and an expired confirmation resolves to not_working.
- NAS: **delegate to the linked NetworkDevice** via `derive_operational_status` when
  `network_device_id` is set (real liveness); else map `status`
  (maintenance/decommissioned/offline→not_working) + `health_status`; an
  unlinked administrative `active` value alone does not prove operation.
  Document this as the one owner of the nas-derived field (SOT).
- Presentation is already covered — both values map in `_DEVICE_OPERATIONAL_PRESENTATIONS`;
  no nas/router-specific presentation needed.

**class_facts per type** (set `device["class_facts"]` in each block):
- ONT: `{onu_rx_dbm, olt_rx_dbm, onu_tx_dbm, signal_updated_at}` (already on the ont row).
- OLT: `{pon_port_count, ont_online, ont_total}` — reuse the grouped-query pattern in
  `network_monitoring.py:_pon_availability_items` (~line 140), regrouped by `OLTDevice.id`,
  with `effective_ont_online_clause` from `app/services/network.ont_status`. One query/pass.
- core: `{site_name, role}` from `NetworkDevice.pop_site` + `role`.
- NAS: `{site_name, health_status, connection_types}`.
- router: `{routeros_version, location}`.

**Tests:** `tests/test_device_projection_reconcile.py` (stubs `collect_devices` via
`_patch_collect` / `_device(...)` — add nas/router rows + class_facts asserts + a
dedup case cheaply, no real fixtures); `tests/test_network_core_devices_contracts.py`
(real-source nas/router projection + dedup + class_facts denormalization).

## b3 — read model + facets/stats

`app/services/device_projection_views.py`: `_row_to_dict` add `"class_facts": row.class_facts`;
`device_projection_stats` add `nas`/`router` count keys; `_apply_filters` already handles
`device_type` generically. Facet option lists (currently hardcoded in
`templates/admin/network/devices/index.html`) add `nas`, `router`. Sort still name/last_seen.
Tests: `tests/test_device_projection_views.py`.

## b4 — UI: unified Device ledger + Device 360

- Rewrite `templates/admin/network/devices/index.html` on the **ledger spine**
  (`components/ui/ledger.html`): `facet_bar` (type facet incl nas/router, status, vendor)
  + `ledger_card`/`ledger_head`/`ledger_row` + `row_drawer`. Render status via
  `status_presentation_badge(device.status_presentation)` (no client tone). Show
  class-specific columns/cells from `class_facts` (type-aware). **Fix the latent bug**:
  `_table_rows.html` renders `device.subscriber.name` but the row carries only a raw
  `subscriber_id` UUID — render the id / resolve server-side, never assume an object.
- Drawer: render from the row (identity/status/ip/vendor/last_seen/actions/class_facts) —
  recon says no extra query needed for the quick peek; add a `device_projection_views`
  get-by-`(type,source_id)` only if the drawer needs more, or render client-side from row
  data. "Open full 360" links to `/devices/{id}`.
- Retire `network-devices/index` (duplicate) + the per-class index tables once the ledger
  carries their columns.
- **Device 360** (archetype B, `record.html` spine): `/devices/{id}` becomes a class-driven
  record hosting the per-class tabs the audit mapped (ONT: Overview/Config/Diagnostics/
  Operations/Hosts/Files; OLT sections; CPE: Overview/TR069; router: Overview/Interfaces/
  Snapshots/PushHistory) — reuse the existing per-class detail partials as tab bodies,
  drop the bespoke `onts/_hero_header` for shared `detail_header`/`subscriber_hero`-style hero.

## Validation each slice
Run from the worktree with the poetry venv python:
`tests/test_device_projection_reconcile.py`, `tests/test_device_projection_views.py`,
`tests/test_network_core_devices_contracts.py`, + `tests/test_web_network_core_devices_views.py`
for the UI. Do NOT run `alembic upgrade` against the shared dev DB from the branch — tests
use `create_all`, which already picks up `class_facts`.
