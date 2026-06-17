# Spec: Automatic End-to-End Network Topology, Customer Path & Outage Awareness

Date: 2026-06-16 · Status: draft for review · Supersedes the 2026-06-16 "Customer Path View" draft

## 1. Goals

Give `dotmac_sub` an automatically-maintained model of the network and each customer's place
in it, used in two directions:

- **Forward (customer → infrastructure).** For any subscription, show the end-to-end path —
  ONT/CPE → access device → basestation (BTS) → aggregation → core — with live status,
  surfaced to support/NOC (phase 1) and, later, a customer-safe selfcare view.
- **Reverse (infrastructure → customers).** For any device or basestation, enumerate the
  customers downstream of it. This is the engine for **outage management**: a NOC operator declares
  an outage against a failing BTS/OLT/uplink, the system computes every affected subscriber,
  suppresses per-customer panic ("known outage on your BTS"), and lets the operator notify them.
  Outages are **declared manually, not auto-detected** — a deliberate choice to avoid false
  positives from Zabbix flaps. Live status is shown as decision support; a human decides it is an
  outage.

The topology stays fresh via an idempotent reconcile against the existing Zabbix — no manual
re-entry of relationships. Live status is never stored as truth; it is warmed into cache by a
background task and read from there (never fetched on the render path).

## 2. Non-goals

- Not a network-management/config tool — read-only topology + status.
- Not replacing Zabbix as the device-graph poller/source of truth.
- Not a general UI overhaul of sub (separate track).
- No live polling by sub on the request path — Zabbix polls; sub reconciles and warms cache.
- Phase 1 does **not** send customer-facing notifications (see §11 — the notification runner is
  currently OFF with a backlog; bulk outage notify is explicitly deferred until that is safe).

## 3. Source-of-truth split (core principle)

| Edge | Owner | Origin |
|------|-------|--------|
| Customer → ONT → OLT/access device | sub | provisioning (`OntAssignment` → `OntUnit` → `OLTDevice`; `Subscription.provisioning_nas_device_id` → `NasDevice`) |
| Device → basestation (BTS) | Zabbix | host-group membership ("X BTS" groups) |
| Device → device (access→agg→core) | Zabbix | the "Network Topology" sysmap (33 nodes / 44 links today) |
| Live up/down/health | Zabbix | API, warmed into sub's cache by a background task (never fetched at render time) |

## 4. Reuse decision: extend the existing topology graph, do not fork it

sub **already has** a topology graph that this feature must reuse rather than duplicate:

- `NetworkDevice` (`app/models/network_monitoring.py`) — `id, name, mgmt_ip, role, device_type,
  status, pop_site_id, is_active`. This is the node table.
- `NetworkTopologyLink` (`network_monitoring.py:688`) — directed `source_device/interface →
  target_device/interface` edges, with `link_role` enum (`access`/`distribution`/`core`/`uplink`/
  `backhaul`/...) and `medium` enum (`fiber`/`wireless`/`ethernet`/...). Unique on the
  4-tuple `(source_device_id, source_interface_id, target_device_id, target_interface_id)`.
- Services `network_topology.py` (graph projection/utilization), `network_map.py` (GeoJSON), web
  routes `web_network_topology.py`.

**PRE-REQUISITE INVESTIGATION (blocks milestone 1):** confirm whether `NetworkDevice`/
`NetworkTopologyLink` are currently populated and maintained, or stale/manual. The whole point of
this feature is that the graph rots when hand-entered. Decision:
- If the existing tables are unmaintained → this feature **becomes their maintainer**: the Zabbix
  reconcile (§6) is the new writer; we add the missing columns and a `source` marker.
- If they are populated by another path that must coexist → reconcile writes only Zabbix-sourced
  rows (tagged `source='zabbix_sync'`) and never touches non-Zabbix rows.

Either way we do **not** introduce parallel `NetworkNode`/`DeviceLink` tables.

## 5. Data model (deltas to existing tables + one new table)

### 5.1 New: `Basestation`
One per Zabbix "X BTS" host group.
- `id` (UUID PK)
- `name`, `zabbix_groupid` (unique — rename-proof), `site_label`
- `lat`, `lon` (nullable, from Zabbix inventory)
- `is_active` (soft-prune flag), `last_synced_at`, timestamps

### 5.2 Columns added to `NetworkDevice` (the node)
- `zabbix_hostid` (string, unique-where-not-null) — the stable reconcile key
- `basestation_id` (FK → `Basestation`, nullable)
- `matched_device_type` + `matched_device_id` (nullable) — link to the provisioning device
  (`OLTDevice` / `NasDevice`), set by the matcher (§6.4)
- `source` (e.g. `zabbix_sync`), `last_synced_at`
- `role` stays, but becomes **inferred default + manual override**; the reconcile must not stomp a
  manually-set role (track `role_locked` or `role_source`).

### 5.3 Columns added to `NetworkTopologyLink` (the edge)
- `source` (`zabbix_sysmap`) so Zabbix-sourced edges are reconcilable and distinct from any
  manually/other-sourced edges.
- `last_synced_at`, `is_active` (soft prune).

`Subscription` / `OntUnit` need **no schema change** — they already reach `OLTDevice`/`NasDevice`,
which the node links to via `matched_device_*`.

## 6. The Zabbix reconcile (idempotent)

Single service `app/services/topology/zabbix_sync.py`, run via a scheduled Celery task (hourly is
ample for the device graph) and on-demand. **Hold a single-flight lock per run** (advisory lock) so
scheduled + on-demand runs cannot overlap and flap the prune step.

Per run:
1. **Auth** via the existing `app/services/zabbix.py` client (Bearer token, auth + reachability
   circuit breakers already built — we inherit "Zabbix down never cascades").
2. **Basestations** ← `hostgroup.get` filtered to `*BTS*` → upsert `Basestation` by `zabbix_groupid`.
3. **Nodes** ← `host.get` (`selectHostGroups`, `selectInventory`, interface IP) → upsert
   `NetworkDevice` by `zabbix_hostid`; set `basestation_id` from the BTS group; set `mgmt_ip` from
   the interface; set `lat/lon` from inventory.
4. **Match to provisioning devices** (the matcher), in priority order — first hit wins:
   1. **By Zabbix host id.** `OLTDevice.zabbix_host_id` already exists; match it directly to the
      node's `zabbix_hostid`. Most reliable, rename- and re-IP-proof. Prefer this always.
   2. **By management IP.** Heterogeneous across device types — match `OLTDevice.mgmt_ip` and
      `NasDevice.management_ip` (the SNMP/SSH IP Zabbix actually monitors; **not** `nas_ip` or
      `ip_address`). Require a **unique** hit: 0 → unmatched; >1 → **ambiguous** (flag, never pick
      first).
   3. **By name** (fallback only, when IP gives 0).
   - **`CPEDevice` has no mgmt-IP field** → it is not matched by IP. CPE association, if needed,
     comes via ONT/serial, not this matcher.
   - Unmatched / ambiguous nodes **persist**, flagged, surfaced in the §9 "topology gaps" report —
     never dropped.
5. **Links** ← `map.get` selements+links for "Network Topology" → resolve selement→host→node →
   upsert `NetworkTopologyLink` tagged `source='zabbix_sysmap'`.
   - **Client gap:** `map.get` is NOT in the Zabbix client `ALLOWED_METHODS` today — add it +
     a `get_maps()` method. (One-time client work; flag in milestone 2.)
6. **Prune:** rows whose Zabbix source vanished are marked `is_active=false` (soft), not deleted.
   Compute prune against the snapshot fetched this run, under the run lock.

Idempotent throughout (upsert by stable Zabbix IDs). First run bootstraps; every run reconciles.
The idempotency test asserts run-twice = no row diff except `last_synced_at`.

**Task wiring (gotcha):** the new task module must be (1) imported in `app/tasks/__init__.py`
**and** added to its `__all__`, and (2) routed in `app/celery_app.py`. Route it to the **`ingestion`**
queue (same home as `monitoring_warm`, which has a live consumer). Do not create a new queue.

## 7. Forward resolution (customer → infrastructure)

`resolve_customer_path(subscription)` returns an ordered chain:
1. **Subscription → access device.**
   - Fiber: `Subscription` → active `OntAssignment` → `OntUnit` → `OLTDevice`. (Note: there is **no
     direct Subscription→OntUnit field**; it goes through `OntAssignment` — "no active assignment"
     is a gap case.)
   - Non-fiber: `Subscription.provisioning_nas_device_id` (or legacy `router_id`) → `NasDevice`.
2. **Device → node → basestation.** device → `NetworkDevice` (via `matched_device_*`) → its
   `Basestation`.
3. **Walk upstream toward core.** BFS over `NetworkTopologyLink`, rooted at designated core
   node(s) (see §8) and treated as **undirected** for traversal — derive direction from
   distance-to-core, not from the stored edge direction (a re-drawn sysmap must not reorder paths).
   Yields `[access → agg → core]`.
4. **Attach status** per node from the warm cache (§10), never a live call.

Returns `{ ont, access_device, basestation, upstream_chain[], status_per_node }`. Any gap
(unmatched device, no active ONT assignment, broken chain) returns a **partial** result + a gap
marker.

## 8. Reverse resolution (infrastructure → customers) — manual outage management

`affected_customers(node_or_basestation)` is the mirror of §7 and the foundation of outage
management:
1. Expand the target to the set of **access/edge nodes** downstream of it (for a Basestation: all
   nodes in that BTS group; for an upstream node: BFS *away* from core over `NetworkTopologyLink`).
2. Map those nodes → provisioning devices (`matched_device_*`).
3. Devices → subscriptions: `OLTDevice` → `OntUnit`s (via `OntAssignment`) → `Subscription`s;
   `NasDevice` → `Subscription`s by `provisioning_nas_device_id`.
4. Return the deduped subscriber set, with the failing node(s) that put them in scope.

**Direction note:** correct reverse traversal needs the same core-rooted graph as §7. Designate
core node(s) by role inference + a small config allow-list; everything else's "downstream" is
"farther from core."

### 8.1 Outage incident model
- New `OutageIncident` (or reuse the existing incident/alert table if one fits) — `id`,
  `root_node_id` / `basestation_id`, `declared_by`, `started_at`, `resolved_at`, `severity`,
  `affected_count`, `status`, `notified` flag, `note`.
- An incident is created by an operator selecting the failing node/basestation in the outage
  console (§9). The system snapshots `affected_count` (and the affected subscriber set) via
  `affected_customers` at declare time, and can re-compute on demand. Editing/resolving is manual.

### 8.2 Why detection is manual (not auto)
Zabbix node-down events flap (link blips, maintenance, partial reachability), and a false auto-
incident would wrongly suppress real per-customer troubleshooting and risk a mistaken customer
blast. So sub does **not** auto-open incidents. Instead:
- Live status from the warm cache (§10) is shown next to each node in the outage console and on the
  customer path panel, using the existing `get_triggers()` (`trigger.get`) data — **use
  `trigger.get`, not `problem.get`** (the latter is not in the client allow-list; the former is
  implemented and batches by `host_ids`). This is decision support, not a trigger.
- A human reads that status and **declares** the outage. The system then does the heavy lifting
  (affected set, banner, notify).
- Auto-detection is a possible future enhancement (threshold + dwell + dedup), explicitly out of
  scope here. If added later, it should *propose* an incident for operator confirmation, never
  declare-and-notify on its own.

## 9. Views

- **Phase 1 — support/NOC (forward).** On the customer/subscription page, a "Network Path" panel:
  ONT → access device → basestation → upstream, each with a status dot read from cache. Read-only.
  If an open `OutageIncident` covers this customer's path, show "Known outage on <BTS> — N
  customers affected" instead of implying it's customer-specific.
- **Admin — topology gaps report.** Unmatched / ambiguous nodes, unmatched roles, subscriptions
  with no resolvable path. The thing you fix instead of silently losing data.
- **Admin — outage console (reverse).** Operator declares an outage by picking a failing node/BTS
  (live status shown as decision support); lists open incidents, root node, affected-customer
  count + list, and a manual notify action (cannot send until §11 lifts the gate).
- **Phase 3 — selfcare (forward, customer-safe).** "You're connected via Garki BTS; status:
  healthy" — no internal IPs/topology internals. During an outage: "Known outage in your area,
  we're on it" — leans on the §8 incident, not a per-customer probe.

## 10. Live status — warm-and-store, never render-fetch

Follow the established house pattern (this is exactly how the admin monitoring dashboard already
works — `OntUnit.olt_status`/`olt_rx_signal_dbm` are refreshed async and the dashboard reads the
local columns, warmed by `app/tasks/monitoring_warm.py` on the `ingestion` queue because the
synchronous fan-out was ~100s):
- A warmer task refreshes per-node status (`get_hosts` + `get_triggers`, batched by `host_ids`)
  into a cached `status` / `last_status_at` on the node row (or Redis, mirroring the dashboard
  cache TTL of ~180s).
- The Network Path panel and outage console read the cached status. No Zabbix call on the request
  path, ever.
- Truly-live dots, if wanted, come from a separate AJAX poll — not the page render.

## 11. Error handling / edge cases

- **Zabbix unreachable:** structure served from sub's tables (they *are* the cache); status panel
  shows "live status unavailable." A global "topology last reconciled Nh ago" staleness banner
  (driven by `last_synced_at`) makes a stuck sync visible. Never blocks the page. (Circuit breakers
  in the client already prevent cascades.)
- **Unmatched / ambiguous node:** persists, flagged, surfaced in the gaps report.
- **Customer with no resolvable device:** view shows "path unknown — provisioning incomplete."
- **Sysmap incomplete (only 33 nodes):** chain renders as far as links exist, then "upstream not
  mapped."
- **BTS / host rename in Zabbix:** matched by `zabbix_groupid` / `zabbix_hostid`, so renames update
  in place.
- **Ring / cycle in agg:** core-rooted undirected BFS terminates on visited-set; explicit test case.
- **Notification runner is OFF with a backlog.** Even the *manual* bulk-notify button is blocked
  from actually sending until the notification queue runner is enabled safely (milestone 5). Until
  then the outage console still shows the affected set and "known outage" banner — it just can't
  send. Dedup so a re-notify on the same incident never double-sends.

## 12. Phasing / milestones

0. **Pre-req:** determine extend-vs-replace for `NetworkDevice`/`NetworkTopologyLink` (§4); add
   `map.get`/`get_maps()` to the Zabbix client.
1. **Forward, basestation-level.** `Basestation` model + node/membership reconcile + matcher
   (zabbix_hostid → IP → name) + support "which basestation + access device" panel. Smallest
   shippable slice. **DONE — shipped 2026-06-17 (PR #274, mig 153); 96.2% match-rate in prod.**
2. **Full forward chain.** Core-rooted directed access→agg→core chain in the panel.
   **DEFERRED 2026-06-17 — blocked, no data source.** A spike confirmed sub-zabbix has *no*
   topology signal: 0 items for `lldp`/`cdp`/`neighbor`/`topology`/`uplink`/`ifalias`/`ifdescr`/
   `sysname`, the sysmap is empty, and `parent_device_id` is 0/461. Monitoring is host-level
   (ping/availability) only, so there is nothing to auto-derive device→device edges from.
   **Prerequisite to unblock:** enable LLDP/CDP collection in Zabbix (SNMP `lldpRemTable` on the
   NAS/switches — an infra/SNMP change outside sub). Once neighbor items exist, the reconcile
   derives edges into `NetworkTopologyLink` and this phase proceeds. Rejected alternatives: manual
   topology entry (breaks the auto-maintained principle) and heuristic role/BTS inference
   (unreliable). The sysmap/`map.get` path in §5/§6 is moot for sub-zabbix (no sysmap).
3. **Live-status overlay (forward) + selfcare view.** Warmer task + cached status + customer-safe
   panel. **DONE — shipped 2026-06-17 (PR #275, mig 155).**
4. **Reverse traversal + manual outage console.** `affected_customers()`, `OutageIncident` model,
   admin outage console (operator declares an outage against a node/BTS → affected set computed),
   "known outage" banner on customer/selfcare views. Notify is a **manual** button (no sending yet
   if the runner is off — see milestone 5).
5. **Customer outage notification (manual-triggered).** Only after the notification runner is on and
   safe: an operator triggers a one-shot bulk notify for an open incident, per-channel (SMS/email/
   push) via the existing notification stack, with dedup so a re-notify doesn't double-send.

## 13. Testing

- **Unit:** matcher (zabbix_hostid match, unique IP match, ambiguous IP, name fallback, CPE
  no-IP, unmatched); `resolve_customer_path` against fixture graphs incl. gaps; `affected_customers`
  reverse traversal; core-rooted direction normalization incl. a ring/cycle fixture; declaring an
  incident snapshots the correct affected set; notify dedup (re-notify doesn't double-send).
- **Integration:** reconcile against recorded Zabbix API responses (golden fixtures) → asserts
  idempotency (run twice = no row diff except `last_synced_at`); declare-outage against a fixture
  graph → asserts the affected subscriber set matches.
- **Manual:** spot-check 5 known customers across fiber + wireless against the Zabbix UI; force one
  known BTS down in a fixture and confirm the affected list matches reality.

## 14. Open decisions

- **A. Phase-1 audience:** support-only first (recommended). Selfcare deferred to phase 3 — the
  model supports it, deferring costs nothing, and it avoids leaking internal topology before the
  customer-safe framing is right.
- **B. Cadence:** hourly scheduled + on-demand for the Zabbix *device graph* (changes slowly);
  resolve the *customer→device* edge live (it's a local FK walk — no Zabbix needed) or invalidate
  on provisioning events. Don't tie graph-sync to provisioning events; they're unrelated triggers.
- **C. Wireless / non-fiber via BTS without OLT/ONT:** `Subscription.provisioning_nas_device_id`
  + `NetworkTopologyLink.medium='wireless'` let the model anchor a wireless sub at a NAS/AP with no
  ONT. **Confirm before freezing milestone 1:** is a wireless sub's access device a real
  `NasDevice`/`NetworkDevice` with a Zabbix host (then §7's non-fiber branch covers it), or is the
  radio sector only a Zabbix host with no provisioning row (then we anchor "lands at BTS X" by
  group, not by device)?
- **D. Who can declare/notify:** which roles may declare an outage and trigger the customer notify
  (RBAC). Outages are manual by decision, so this is the control that matters — not an auto
  threshold. (Auto-detection stays out of scope to avoid false positives; if ever added, it only
  *proposes* an incident for operator confirmation.)

---

## Amendments (2026-06-16, post-validation) — these SUPERSEDE the sections noted

1. **Source = sub-zabbix, NOT the network Zabbix.** The reconcile reads `dotmac_zabbix_*`
   (the Zabbix stack on the sub host, `http://zabbix-web:8080/api_jsonrpc.php` — the one sub's
   `zabbix.py` already uses and `OLTDevice.zabbix_host_id` already references). The network Zabbix
   (160.119.127.193) is a *duplicate* and is out of scope here. (Supersedes §3, §6 targets.)
2. **Reuse `pop_sites` (23 rows) as the basestation — do NOT add a `Basestation` table.**
   Reconcile `pop_site ↔ Zabbix BTS group` (not 1:1 — `pop_sites` also has regions Abuja/Lagos/CBD);
   populate the existing `network_devices.pop_site_id`. (Supersedes §5.1.)
3. **The 461 `network_devices` are orphaned Splynx data (369 via `splynx_monitoring_id`); Splynx is
   now decommissioned.** Reconcile = **match-merge** existing rows to sub-zabbix hosts by IP/name and
   **backfill `zabbix_hostid`** — NOT a blind upsert (which would duplicate). (Refines §4, §6.3.)
4. **Matcher: sub-zabbix has ~2 hosts per access IP** — the device host (in the `*BTS*` group) AND a
   `NAS: <site>` host (in group `DotMac/Network/NAS`), both on the same IP. So IP-match is ambiguous
   by default: disambiguate `OLTDevice`→device host, `NasDevice`→`NAS:` host (by group/name), not
   "unique IP." (Refines §6.4.)
5. **Role inference from sub-zabbix's structured groups** (no sysmap needed): `*BTS*` = access-site,
   `DotMac/Network/NAS` = NAS/BNG, `Data Center Devices` = core/edge.
6. **Phase 2 directed-chain gap:** sub-zabbix's "Local network" sysmap is EMPTY (1 element / 0 links).
   The populated directed graph (33 nodes / 44 links) exists ONLY in the network Zabbix's "Network
   Topology" map, hand-drawn. So Phase 2 must first import/maintain that map in sub-zabbix (or derive
   a coarse hierarchy from groups, or defer). **Retiring the network Zabbix is gated on migrating that
   sysmap into sub-zabbix first** — it is the one topology asset sub-zabbix lacks. (Sharpens §6.5, §12.)
7. **Phasing:** Phase 1 (basestation + access device) is fully sourced from sub-zabbix and is the
   first shippable slice. The directed chain (Phase 2) and outage management (Phases 4–5) are
   separate decisions/specs, gated on items 6 above.
