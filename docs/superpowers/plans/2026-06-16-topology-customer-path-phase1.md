# Topology Customer-Path — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development to implement task-by-task. Each task subagent MUST first read the live files named in **Files** (signatures/patterns are authoritative there — do not invent them). Steps use `- [ ]` checkboxes.

**Goal:** Show, on a subscription, which **access device + basestation (BTS)** the customer is connected to — reconciled automatically from **sub-zabbix** (the local `dotmac_zabbix_*` the app already uses).

**Architecture:** A new *pull* reconcile reads sub-zabbix host-groups (`*BTS*`) + host membership and writes onto the EXISTING tables — `pop_sites` (= basestation) and `network_devices.pop_site_id` — never new parallel tables. A matcher links sub-zabbix hosts to provisioning devices (`OLTDevice`/`NasDevice`). `resolve_customer_path()` walks `Subscription → OntAssignment → OntUnit → OLTDevice` (fiber) or `provisioning_nas_device_id → NasDevice` (non-fiber) → `network_device → pop_site`. A read-only panel renders it. Live status is out of Phase 1 (Phase 3).

**Tech Stack:** Python/FastAPI, SQLAlchemy, Alembic, Celery (`ingestion` queue), pytest. Reuses `app/services/zabbix.py` (`ZabbixClient.from_env()`, circuit-breakered), `app/services/network_topology.py`, `app/services/zabbix_host_sync.py` (mirror its structure, opposite direction).

---

## Decisions locked (from spec + 2026-06-16 amendments)
- Source = **sub-zabbix** (`http://zabbix-web:8080`), NOT the network Zabbix.
- Reuse `pop_sites` as basestation (cols incl. `code, latitude, longitude, is_active`); add `zabbix_group_id`. Do NOT add a `Basestation` table.
- `network_devices` is pre-populated (461; 369 Splynx-sourced, now orphaned) → reconcile is **match-merge + backfill `zabbix_hostid`**, NOT blind upsert.
- Matcher must disambiguate **~2 sub-zabbix hosts per IP** (device host in `*BTS*` group vs `NAS: X` host in `DotMac/Network/NAS`): `OLTDevice`→device host, `NasDevice`→`NAS:` host.
- Role inference from groups: `*BTS*`=access-site, `DotMac/Network/NAS`=nas, `Data Center Devices`=core.
- Phase 2 (directed chain) is OUT — sub-zabbix sysmap is empty; separate decision.

## File structure
- **Create** `app/services/topology/__init__.py`
- **Create** `app/services/topology/zabbix_reconcile.py` — the pull reconcile (groups→pop_sites, hosts→network_devices membership, matcher). One responsibility: bring Zabbix structure into sub tables, idempotently.
- **Create** `app/services/topology/customer_path.py` — `resolve_customer_path(db, subscription)` read path. Pure read; no Zabbix call.
- **Create** `app/tasks/topology_sync.py` — Celery task `run_topology_reconcile`, routed to `ingestion`.
- **Modify** `app/services/zabbix.py` — ensure `host.get` is in `ALLOWED_METHODS`; add `get_hosts(group_ids=None, with_groups=True, with_interfaces=True, with_inventory=False)` mirroring `get_host_groups`.
- **Modify** `app/models/network.py` — add `zabbix_hostid`, `source`, `last_synced_at`, `role_source` to `NetworkDevice` (P4 deltas).
- **Modify** `app/models/<pop_site model>.py` — add `zabbix_group_id` (nullable, unique-where-not-null) to `pop_sites`.
- **Create** alembic migration for the two model deltas.
- **Modify** `app/tasks/__init__.py` (import + `__all__`) and `app/celery_app.py` (route `app.tasks.topology_sync.run_topology_reconcile` → `ingestion`).
- **Create** web: a `resolve_customer_path` panel — extend the existing subscription detail route/template (find via `grep -rn "subscription" app/web*`); reuse `web_network_topology.py` patterns.
- **Create** admin "topology gaps" view (unmatched/ambiguous/no-path).
- **Tests:** `tests/services/topology/test_zabbix_reconcile.py`, `test_customer_path.py`, `tests/services/topology/test_matcher.py`.

## Task breakdown (TDD; commit after each)

### Task 1 — Schema deltas + migration
- [ ] Read `app/models/network.py` (`NetworkDevice`) and the `pop_sites` model for exact base/mixins.
- [ ] Add columns: `NetworkDevice.zabbix_hostid: str|None (unique where not null)`, `source: str|None`, `last_synced_at: datetime|None`, `role_source: str|None`; `PopSite.zabbix_group_id: str|None (unique where not null)`.
- [ ] `alembic revision --autogenerate -m "topology: zabbix linkage cols"`; review the migration (no unrelated drops).
- [ ] Run migration in a scratch DB; assert columns exist. Commit.

### Task 2 — Zabbix client: `get_hosts`
- [ ] Read `app/services/zabbix.py` `get_host_groups` for the exact `_submit_read_payload` pattern + `ALLOWED_METHODS`.
- [ ] **Test first** `test_get_hosts_builds_host_get_payload`: mock `_submit_read_payload`, assert method `host.get`, params include `selectHostGroups`, `selectInterfaces`, and `groupids` when passed.
- [ ] Ensure `host.get` ∈ `ALLOWED_METHODS`; implement `get_hosts(...)` mirroring `get_host_groups`.
- [ ] Run test → pass. Commit.

### Task 3 — Matcher (`zabbix host → provisioning device`)
- [ ] Read `OLTDevice` (`zabbix_host_id`, `mgmt_ip`) and `NasDevice` (`management_ip`) fields.
- [ ] **Test first** `test_matcher` cases: (a) OLT by `zabbix_host_id` exact; (b) NAS by unique `management_ip`; (c) two sub-zabbix hosts same IP → pick device-host for OLT / `NAS:`-named host for NAS by group; (d) 0 hits → unmatched; (e) >1 ambiguous after disambiguation → flagged, not picked.
- [ ] Implement `match_host(db, zhost) -> (device_type, device_id) | ('unmatched'|'ambiguous', None)` in `zabbix_reconcile.py`. Priority: `zabbix_host_id` → group-disambiguated IP → name.
- [ ] Run → pass. Commit.

### Task 4 — Reconcile: groups→pop_sites, hosts→network_devices
- [ ] **Test first** `test_reconcile_idempotent`: feed a fixture of 2 BTS groups + 3 hosts (incl. a device+NAS pair on one IP) → assert pop_sites upserted by `zabbix_group_id`, network_devices get `pop_site_id`+`zabbix_hostid`+`source='zabbix_reconcile'`, matcher links set, and **running twice changes nothing but `last_synced_at`**.
- [ ] Implement `reconcile(db, client)`: pull `*BTS*` groups → upsert `pop_sites`; pull hosts (`get_hosts`) → for each, find-or-merge `NetworkDevice` by IP/name (backfill `zabbix_hostid`; do NOT create a dup if a Splynx-sourced row matches), set `pop_site_id` from its BTS group, run matcher. Soft-prune (`is_active=false`) rows whose Zabbix source vanished, computed against this run's snapshot under an advisory lock.
- [ ] Run → pass (incl. idempotency). Commit.

### Task 5 — Celery wiring
- [ ] Read an existing task (`app/tasks/zabbix_ingestion.py`) for the decorator/name pattern.
- [ ] Create `run_topology_reconcile` calling `reconcile(db, ZabbixClient.from_env())`; import it in `app/tasks/__init__.py` + add to `__all__`; route to `ingestion` in `app/celery_app.py`.
- [ ] Test the task imports + is registered. Commit.

### Task 6 — `resolve_customer_path`
- [ ] Read `Subscription`, `OntAssignment`, `OntUnit`, `OLTDevice` relationships (no direct Subscription→OntUnit — goes via OntAssignment).
- [ ] **Test first** `test_resolve_path` cases: fiber (sub→ont→olt→node→pop_site), non-fiber (sub.provisioning_nas_device_id→nas→node→pop_site), gap (no active OntAssignment → partial+`gap='no_ont'`), unmatched device (`gap='no_node'`).
- [ ] Implement `resolve_customer_path(db, subscription) -> {ont, access_device, basestation, gap?}` in `customer_path.py`. Pure read.
- [ ] Run → pass. Commit.

### Task 7 — Customer panel (read-only)
- [ ] Find the subscription detail route/template (`grep -rn "subscription_detail\|/subscriptions/" app/`); mirror `web_network_topology.py` render style.
- [ ] Add a "Network Path" panel: ONT → access device → **basestation** (no status in Phase 1). On gap, show the gap message.
- [ ] Smoke test the route returns 200 with the panel for a known sub. Commit.

### Task 8 — Topology gaps report (admin)
- [ ] **Test first** `test_gaps`: counts of unmatched/ambiguous network_devices + subscriptions with `resolve_customer_path` gap.
- [ ] Implement `topology_gaps(db)` + a simple admin view listing them. Commit.

## Exit criteria (Phase 1)
- Reconcile run twice = no diff except `last_synced_at` (idempotency test green).
- Match-rate report ≥ agreed threshold; remaining in gaps report (not silently dropped).
- Panel renders correct basestation for ≥5 spot-checked known customers (fiber + NAS).

## Out of scope (later phases / separate specs)
- Directed chain / sysmap (Phase 2 — sub-zabbix map empty), live status overlay (Phase 3), reverse traversal + outage console + notifications (Phases 4–5).
