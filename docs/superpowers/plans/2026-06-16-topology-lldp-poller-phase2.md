# Topology Phase 2 — LLDP Neighbor Poller → directed links

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Each task subagent MUST read the live files named in **Files** for exact signatures. Steps use `- [ ]`.

**Goal:** Populate the (currently empty) directed device graph `NetworkTopologyLink` automatically by polling each MikroTik NAS's `/ip/neighbor` — a durable replacement for the empty/hand-drawn sysmap. Builds on PR #274 (Phase 1 basestation view) and the now-enabled `lldp-infra` discovery.

**Architecture:** A *pull* poller connects to each NAS (reusing `router_management/connection.py`), reads `/ip/neighbor`, **matches each neighbor to a known `network_device`** (by identity → mgmt-IP; drops empty-identity CPEs + unmatched), builds **device-level** edges, dedups bidirectional A↔B to one canonical edge, and upserts `NetworkTopologyLink(source='lldp_neighbor')`. Idempotent + soft-prune. No router writes (discovery already enabled separately).

**Tech Stack:** Python/SQLAlchemy/Alembic/Celery(`ingestion`)/pytest. Reuses `router_management/connection.py` (MikroTik client), `app/services/network_topology.py` (link CRUD), PR #274's `network_devices.matched_device_*` (NAS↔node link).

---

## Decisions locked
- **Discovery is already enabled** fleet-wide via `lldp-infra` (internal physical only; external transit/IXP/peer excluded). The poller does NOT touch router config — read-only `/ip/neighbor`.
- **Match priority:** (1) neighbor `identity` → `network_device.name`/`hostname` (normalized: lowercase, collapse space/hyphen); (2) neighbor `address4` → `network_device.mgmt_ip`. **Empty identity OR no match → dropped** (CPE/unknown), counted in a gaps stat.
- **Device-level links:** `source_interface_id`/`target_interface_id` = NULL (we know the local interface name but have no Port row / no remote interface from `/ip/neighbor`); store local interface + remote board in `metadata`. Unique on the 4-tuple → one link per device pair with NULL interfaces.
- **Dedup bidirectional:** canonicalize each edge as `(min(uuid), max(uuid))` so A→B and B→A collapse to one row.
- **`medium`** inferred from local interface type: `sfp*`→`fiber`, `ether`→`ethernet`. **`link_role`** left default/`uplink` (refined later).
- **Provenance + prune:** add `NetworkTopologyLink.source`; the poller owns `source='lldp_neighbor'` rows only — never touches manual/other rows; soft-prune (`is_active=false`) LLDP rows not seen this run.

## File structure
- **Modify** `app/models/network_monitoring.py` — add `NetworkTopologyLink.source` (str, nullable) + `last_seen_at`.
- **Create** alembic migration (`154_...`) for those two columns.
- **Create** `app/services/topology/lldp_poller.py` — `poll_all(session)` + `match_neighbor(session, nb) -> network_device|None` + `_canonical_edge`. Pure: connect, read, match, upsert, prune.
- **Modify** `app/services/topology/__init__.py` — export.
- **Create** `app/tasks/topology_lldp.py` — Celery `run_lldp_topology_poll`, routed to `ingestion`; import in `app/tasks/__init__.py` (+`__all__`), route in `app/celery_app.py`.
- **Create** `scripts/one_off/lldp_poll_dryrun.py` — preview edges/matches/gaps without writing.
- **Tests:** `tests/services/topology/test_lldp_matcher.py`, `test_lldp_poller.py`.

## Tasks (TDD; commit each)

### Task 1 — schema + migration
- [ ] Read `NetworkTopologyLink` in `app/models/network_monitoring.py`.
- [ ] Add `source: Mapped[str|None]` + `last_seen_at: Mapped[datetime|None]`.
- [ ] `alembic revision --autogenerate -m "topology: link source + last_seen"`; review (additive only); apply to scratch DB. Commit.

### Task 2 — neighbor matcher
- [ ] Read `network_devices` (`name`, `hostname`, `mgmt_ip`).
- [ ] **Test first** `test_match_neighbor`: (a) identity "Gwarimpa Access" → the device named "Gwarimpa Access" (normalized); (b) identity "" → None; (c) no identity match but `address4` == a device `mgmt_ip` → that device; (d) neither → None.
- [ ] Implement `match_neighbor(session, nb)` — normalized identity match, then mgmt_ip; else None. Commit.

### Task 3 — edge build + dedup + medium
- [ ] **Test first** `test_edges`: given a NAS node + 3 neighbors (one matched MikroTik core via identity, one CPE empty-identity, one switch matched by IP), assert: 2 edges built (CPE dropped), each canonicalized `(min,max)` device pair, medium='fiber' for an `sfp` interface, local-interface recorded in metadata, A↔B dedups to 1.
- [ ] Implement `_canonical_edge(a_id,b_id,...)` + edge accumulation with dedup-by-pair. Commit.

### Task 4 — poller upsert + prune (idempotent)
- [ ] Read `router_management/connection.py` for how to open a NAS connection + run a read command (`/ip/neighbor`).
- [ ] **Test first** `test_poll_all_idempotent`: stub the connection to return a fixed `/ip/neighbor` per NAS; assert links upserted by canonical pair with `source='lldp_neighbor'`, **run-twice = no new rows** (only `last_seen_at` bumps), and an edge that vanishes is soft-pruned (`is_active=false`).
- [ ] Implement `poll_all(session)`: iterate NAS network_devices with a mgmt path → connect → read `/ip/neighbor` → match → build/dedup edges → upsert (by canonical pair, source-scoped) → prune unseen LLDP rows. Per-NAS failures isolated (one unreachable NAS — e.g. karsana — never aborts the run; counted). Commit.

### Task 5 — Celery wiring + dry-run
- [ ] Read `app/tasks/zabbix_ingestion.py` for the task/name pattern.
- [ ] Create `run_lldp_topology_poll` → `poll_all`; import + `__all__` in `app/tasks/__init__.py`; route to `ingestion` in `app/celery_app.py`.
- [ ] Create `scripts/one_off/lldp_poll_dryrun.py` (prints matched/dropped/edges, no writes). Test task registration. Commit.

## Exit criteria
- Dry-run on the live fleet shows the expected infra edges (e.g. spdc→GBB, spdc→Gwarimpa, spdc→SPDC-Switch) and drops CPEs.
- Run-twice idempotent (no row churn except `last_seen_at`).
- `NetworkTopologyLink` goes from 0 → the real directed graph; PR #274's `resolve_customer_path` can then walk it for the upstream chain (separate view task).

## Out of scope (later)
- Walking the links into the customer-path **panel** (the directed-chain UI) — follow-on.
- Huawei OLT / non-MikroTik native polling (they're still *seen* by adjacent MikroTiks via LLDP).
- `link_role` refinement (core/distribution/access) + capacity.
