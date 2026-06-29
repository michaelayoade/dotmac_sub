# Spec: Infrastructure Performance & SLA Dashboards (AP / PON / OLT / BTS)

Date: 2026-06-26 · Status: draft for review · Owner: NOC/Network ops

## 1. Goals

Give operators a single place to answer **"where in the network have we been having
challenges?"** across every infrastructure tier, in three modes:

1. **Live wallboard** — what is up / down / degraded *right now*, per tier (BTS, OLT, PON,
   access-point), at a glance.
2. **Historical worst-performer ranking** — over a chosen window (24h / 7d / 30d), which BTS,
   OLT, PON port, or AP has accumulated the most downtime / incidents / lowest uptime %, so
   investment goes where it hurts.
3. **SLA compliance** — per-element uptime % against a target (e.g. 99.5%), with breach flags and
   (later) credit computation, driving the inert `SlaProfile` model into use.

The four tiers the dashboard must cover, and how each maps onto existing models:

| Tier (user term) | Backing model(s) | Already a reporting dimension? |
|------------------|------------------|--------------------------------|
| **Entire BTS / basestation** | `PopSite` (Zabbix "X BTS" group) | ✅ `uptime_report group_by="pop_site"` |
| **Entire OLT** | `OLTDevice` ↔ `NetworkDevice` (`matched_device_type="olt"`) | ✅ `group_by="device"` |
| **OLT PON port** | `PonPort` (`app/models/network.py:1533`) | ❌ **new dimension** |
| **Access point (wireless radio)** | `NetworkDevice device_type="access_point"`, under `WirelessMast` → `PopSite` | ⚠️ device-level only; **no AP roll-up** |

## 2. Non-goals

- Not a replacement for Zabbix as the poller/source of truth — sub reconciles + warms, never
  polls on the render path (same principle as `NETWORK_TOPOLOGY_CUSTOMER_PATH.md`).
- Not auto-declaring outages. Outage incidents stay **manually declared** (`OutageIncident`); SLA
  math here is alert/availability-derived and is decision-support, not an outage trigger.
- Not a network-config tool — read-only performance/SLA views.
- No new wireless radio/sector/antenna model in phase 1 — APs are existing
  `NetworkDevice device_type="access_point"` rows (see §5.3).

## 3. Source-of-truth split (core principle)

Two *different* truths already exist and must not be conflated — the dashboard uses each for a
different surface:

| Signal | Owner | Used for |
|--------|-------|----------|
| **`NetworkDevice.live_status`** (up/down/problem/unknown) — Zabbix availability warmed into cache by `warm_topology_status()` | Zabbix → cache | **Live wallboard** (surface 1) |
| **`Alert` records, `metric_type=uptime`** — triggered_at→resolved_at intervals | Zabbix triggers → webhook → AlertRule → Alert | **Historical uptime % + SLA** (surfaces 2 & 3) via `uptime_report()` |
| **`OutageIncident`** (manual, start/end, affected_count) | Operator | Incident overlay + MTTR |

The existing `uptime_report()` (`app/services/network_monitoring.py:113`) is the SLA engine and
**already computes uptime %** by merging downtime intervals — we extend it, we do not rebuild it.

## 4. Reuse decision: extend the existing engine, add two dimensions + one snapshot table

What we reuse as-is:
- `uptime_report()` interval-merge math and `group_by` dispatch (device / pop_site / area / fdh).
- `web_network_monitoring.py` — `get_pon_outage_summary()`, `get_onu_status_summary()`,
  `_get_device_health_table()`, the warmed per-OLT ONT cache.
- `topology/affected.py` `affected_customers()` — to attach **customer blast-radius** to every
  ranked element ("this OLT's downtime affected 312 subscribers").
- Snapshot pattern proven by `IpPoolUtilizationSnapshot` and `MrrSnapshot` (model + daily Celery
  task + retention prune) — copy it for availability.
- `SlaProfile` (uptime_percent / response_time_hours / credit_percent) — finally consumed.

What we add (deltas, no parallel tables):
- `group_by="pon"` and `group_by="access_point"` in `uptime_report()`.
- One new table: `AvailabilitySnapshot` (daily rolled-up uptime % per element — see §5.1).
- MTTR/MTBF aggregation over `Alert` + `OutageIncident` (§6.3).

## 5. Data model

### 5.1 New: `AvailabilitySnapshot`
One row per element per day, so historical ranking/trends don't recompute interval merges over
the whole `Alert` table on every page load (the on-the-fly path is fine for a single ad-hoc
window but not for 365-day trend charts).
- `id` (UUID PK)
- `element_type` (enum: `device` / `pop_site` / `pon_port` / `access_point` / `olt`)
- `element_id` (UUID — FK target depends on type; store id + type, not a polymorphic FK)
- `snapshot_date` (date)
- `uptime_percent` (Numeric 5,2), `downtime_seconds` (int), `window_seconds` (int)
- `incident_count` (int — alerts opened in window), `affected_subscribers_peak` (int, nullable)
- unique `(element_type, element_id, snapshot_date)`; index on `(element_type, snapshot_date)`
- Populated by `snapshot_infrastructure_availability()` Celery task (daily, ~04:30, after the
  day closes). Retention ~400 days, pruned by a sibling task — mirror `ip_utilization.py`.

### 5.2 New SLA dimension: PON port
`PonPort` has no Zabbix host and no uptime `Alert` source today. PON "down" is currently derived
point-in-time by `get_pon_outage_summary()` from ONT `olt_status`. Two options (decide in review):
- **(A) Derive** — at snapshot time, compute PON availability from the **fraction of its ONTs
  online** (or the parent OLT's uptime gated by per-port ONT-offline bursts). Cheap, no new
  ingestion, approximate. **Recommended for phase 1.**
- **(B) First-class alerts** — emit `Alert(metric_type=uptime, ...)` keyed to a PON when all/most
  ONTs on a port drop simultaneously, so PON flows through the exact same engine. More faithful,
  needs a detector task. Phase 2.

### 5.3 Access-point (wireless radio) roll-up
APs are `NetworkDevice` rows with `device_type="access_point"`, attached to a `PopSite`, optionally
under a `WirelessMast` (`app/models/wireless_mast.py`). No schema change needed — `group_by=
"access_point"` simply **filters** `NetworkDevice` to `device_type=access_point` and reports each
as a device, with optional roll-up to its `WirelessMast` / `PopSite`. (Confirm in review whether
ops wants per-radio rows, per-mast roll-up, or both.)

## 6. Behaviour

### 6.1 Surface 1 — Live wallboard (`/admin/network/performance` or a tab on monitoring)
Tier cards (BTS / OLT / PON / AP), each: count up/down/degraded/unknown from cached `live_status`
(+ `get_pon_outage_summary` for PON, + ONT-online ratio per OLT). Click a tier → drill to the
element list with current status, last-change time, and live customer-impact count. **Reads only
warmed cache** — no Zabbix fan-out on the request path.

### 6.2 Surface 2 — Worst-performer ranking
A window picker (24h/7d/30d/custom) → `uptime_report()` for the chosen tier, **sorted ascending by
uptime %** (worst first), columns: element, uptime %, total downtime, incident count, peak
affected subscribers, sparkline (from `AvailabilitySnapshot`). This is the "where have we been
having challenges" view. CSV export (reuse `reports.py` export pattern).

### 6.3 Surface 3 — SLA compliance + MTTR
Per element: uptime % vs `SlaProfile.uptime_percent` target → **PASS / BREACH** badge, breach
margin, and **MTTR** = mean(resolved_at − triggered_at) over uptime `Alert`s in window (and/or
over `OutageIncident`s for declared events). MTBF = window / incident_count. Credit computation
(`SlaProfile.credit_percent`) is **display-only in phase 1**; wiring it to `ServiceExtension`
(the existing outage-compensation engine) is a later, gated step.

## 7. Phasing

- **Phase 0 (prerequisite — blocks everything):** verify the **uptime-`Alert` pipeline is actually
  populated.** `uptime_report()` is only as truthful as `Alert(metric_type=uptime)` rows. Confirm
  Zabbix availability triggers → `zabbix_webhook` → `AlertRule` → `Alert` produces uptime alerts in
  prod. If not, the SLA numbers are hollow until that path is fixed/seeded. (This is the analogue
  of the topology doc's "is the graph maintained?" gate.)
- **Phase 1:** `group_by="access_point"` + `group_by="pon"` (derive, §5.2-A) in `uptime_report()`;
  worst-performer ranking UI (surface 2) for all four tiers; live wallboard (surface 1) from
  existing caches; attach `affected_customers()` blast-radius.
- **Phase 2:** `AvailabilitySnapshot` table + daily task + retention prune → trend sparklines /
  365-day charts; SLA PASS/BREACH + MTTR/MTBF (surface 3).
- **Phase 3 (gated):** first-class PON alerts (§5.2-B); SLA credit → `ServiceExtension` automation;
  customer-safe per-element status (reuse selfcare debounce).

## 8. Risks / open questions

- **R1 — Hollow SLA (highest risk).** If uptime alerts aren't flowing, every uptime % reads
  ~100%. Phase 0 must confirm before any UI promises SLA numbers.
- **R2 — `fdh`/`area` grouping is region-string-matched** (loose join via `pop_site.region`); PON
  derivation (§5.2-A) is approximate. Label these as estimates in the UI, not contractual SLA.
- **R3 — Two truths drift.** Live wallboard (`live_status`) and historical uptime (`Alert`s) can
  disagree (debounce, warmer staleness ≥600s). Show both deliberately; don't try to unify.
- **Q1 — APs:** per-radio rows, per-mast roll-up, or both? (§5.3)
- **Q2 — PON:** derive from ONT-online ratio (A) or build PON alerts (B) for phase 1? (§5.2)
- **Q3 — SLA targets:** one global target, or per-`SlaProfile` per tier/plan?
- **Q4 — Where does it live:** new `/admin/network/performance` page vs tabs on the existing
  `/admin/network/monitoring`?

## 9. Key file references

| Thing | Path |
|-------|------|
| SLA/uptime engine (extend here) | `app/services/network_monitoring.py:113` |
| Uptime API | `app/api/domains_monitoring.py:47` |
| Live status warmer | `app/services/topology/live_status.py` |
| PON / ONT summaries | `app/services/web_network_monitoring.py` (`get_pon_outage_summary`, `get_onu_status_summary`) |
| Customer blast-radius | `app/services/topology/affected.py:155` |
| Models — PopSite / NetworkDevice / Alert | `app/models/network_monitoring.py` (112 / 206 / 602) |
| Models — OLT / PonPort / OntUnit | `app/models/network.py` (811 / 1533 / 1676) |
| WirelessMast | `app/models/wireless_mast.py` |
| Outage incidents + console | `app/models/network_monitoring.py:829` · `app/web/admin/network_monitoring.py` |
| Snapshot pattern to copy | `app/tasks/ip_utilization.py` + `app/models/network.py:771` |
| Inert SLA model | `SlaProfile` |
| Network report page (UI pattern) | `app/web/admin/reports.py:274` |
