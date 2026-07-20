# Network Admin IA Rationalization — proposal

**Status:** PROPOSED — pending Michael's approval. Companion to
`docs/design/ADMIN_UI_ARCHETYPES.md`. This decides *what network surfaces should
exist* before any are rebuilt, so we consolidate rather than repaint.

## Headline

The network admin section is **218 templates** (159 full pages + 59 partials)
across 34 route modules. But the **source-of-truth owners already exist**:
`device_projection_views.py` (one unified device projection),
`device_operational_status.py` (observation-vs-decision reconciler /
`mismatch_worklist`), and `status_presentation_badge` / `.status-tone-*` (the
server-owned tone contract).

The sprawl is almost entirely UI:
- the **same owned entities are projected through 6–9 parallel surfaces each**
  (e.g. `devices/index`, `core-devices/index`, `network-devices/index`,
  `olts/index`, `onts/index`, `cpes/index`, `nas/index`, `routers/index` are
  eight ledgers over a projection that is *already unified*);
- **185 of 214 templates re-derive status color in-template** (`status=='active'
  → emerald`, `severity=='critical' → rose`, `color="amber"`…) instead of
  consuming the tone contract — a near-universal violation of the §2/§3
  color-ownership decisions adopted 2026-07-20.

**Therefore: rationalization = pointing the UI at owners that already exist and
collapsing parallel surfaces. Not a re-ownership project.**

## Target IA — 159 page templates → ~8 designed surfaces + ~43 flattened forms

| # | Target surface | Archetype | Absorbs / retires |
|---|---|---|---|
| 1 | **Network Canvas** | C · Canvas | `network/map`, `fiber/map`, `gis/index`, `topology/index`, `weathermap`, `topology_gaps`, `fiber/field_verification_map`, fiber coverage maps — one full-bleed canvas with toggleable layers (topology / fiber plant / GIS / weathermap / outage heat) + on-demand inspector (~9 → 1) |
| 2 | **NOC / Monitoring** | A · Triage | `monitoring/index`+`alarms`, `outages`, `detected_outages`(+notify), `device_status_worklist`, `sessions`, `radius_errors`; `outage_impact` becomes the inspector body (~9 → 1). The one genuinely archetype-A surface. |
| 3 | **Device ledger** | D · Ledger | `devices/index`, `core-devices/index`, `network-devices/index`, `olts/index`, `onts/index`, `cpes/index`, `nas/index`, `routers/index`; device-groups as a facet (~8 → 1) |
| 4 | **Device 360** | B · Record | `onts/detail` (+~40 partials as tabs), `olts/detail` (+8), `core-devices/detail`, `cpes/detail`, `routers/detail`, `nas/device_detail`, `uisp-control/detail`, `pop-sites/detail`, `zones/detail`, `vlans/detail` → one class-driven Device-360 family (~10 pages + ~55 partials → 1) |
| 5 | **Fiber plant ledger** | D · Ledger | `fiber/{fdh-cabinets,splice-closures,splitters,strands,change_requests,ont_identity_reviews,reports}`; spatial part folds into the Canvas (#1) (~10 lists → 1 + forms) |
| 6 | **IPAM ledger** | D · Ledger | `ip-management/*` (networks v4/v6, pools, assignments, addresses, PD, dual-stack); keep `calculator` as a utility (~17 → ~2) |
| 7 | **Backups ledger** | D · Ledger | top-level `backups/index` + the `core-devices`/`olts`/`nas` backup+detail+compare triplets (one owner, `connectivity_backup.py`); also surfaced as a Device-360 tab (~10 → 2) |
| 8 | **Config / catalog ledgers** | D · Ledger | firmware, onu-types, speed-profiles, radius, tr069 presets/provisions/acs, vendor-capabilities, uisp-control, speedtests, authorization-presets, zones, vlans, pop-sites, router templates — the CRUD long tail on a shared `data_table` + row-drawer + form (~30 idx/form pairs) |

## Redesign vs. mechanically flatten

- **Worth redesigning** (real archetype thinking): #1 Canvas, #2 NOC, #3 Device
  ledger, #4 Device 360, #5 Fiber, #6 IPAM.
- **Mechanical port only** (move to `data_table` + row drawer + the tone macro,
  delete hardcoded colors — hygiene, not archetype work): everything in #7/#8,
  the ~43 `*form*` templates, and the small idx/detail/form triples.

## Retire outright

`network-devices/index` (duplicate of `devices/index`); the per-class device
index tables once the Device ledger lands; the duplicated backup detail/compare
triplets; the bespoke `onts/_hero_header` (use shared `detail_header`); 4 of the
5 standalone maps.

## SOT violations to fix during migration (examples)

- **UI re-derives tone from a status string** (§2 color-as-meaning): `onts/_tab_overview.html:37`, `monitoring/alarms.html:82`, `monitoring/index.html:208`, `outage_impact.html:116`, `fiber/trace.html:63`, `core-devices/_interfaces_card.html:70`, `dns_threats/index.html:125`, `nas/device_detail.html:588`. Prevalence: **185/214 templates**. Fix: consume `status_presentation_badge` / server tone.
- **Business logic + tone decided client-side** (§1/§5): `cpes/_tr069_partial.html:344,372` — inline `@click` builds a `fetch()` POST and decides success/error tone in Alpine. Fix: server owns the action + result tone.
- **Parallel surface over one owner** (§5): `network-devices/index` is a second ledger from the same service backing `devices/index`.

## Recommended migration order

The missing reusable spine is the **data-ledger (archetype D)**; building it first
unlocks surfaces 3, 5, 6, 7, 8. Spines already built: triage (A), record (B),
canvas partial (C, topology uplifted §3).

1. **Data-ledger spine** (D) — shared `data_table` + row-drawer + facet filter.
2. **Device ledger (#3) + Device 360 (#4)** — biggest consolidation; reuses ledger + `record.html`.
3. **Network Canvas layers (#1)** — extend the §3 topology canvas with layers; retire the other maps.
4. **NOC / Monitoring (#2)** — reuse `triage.html`.
5. **Fiber (#5), IPAM (#6), Backups (#7), Config (#8)** — reuse the ledger spine; mostly mechanical.

## Open decisions for Michael

- Approve this target IA (or adjust which surfaces merge)?
- Build order — as recommended, or a specific NOC/operator priority first?
- Retire-vs-keep calls on any surface you rely on that the audit proposes merging.
