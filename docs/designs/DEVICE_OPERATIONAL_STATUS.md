# Spec: Device Operational Status (derived NOC truth, not a field swap)

Date: 2026-06-26 · Status: draft for review · Owner: NOC/Network ops

## 1. Problem

The Network Devices page (`/admin/network/network-devices`) renders
`device.status` — an **administrative/inventory** field set by admins and
lifecycle automation. It does not reflect reality. In prod right now, admin
status disagrees with live monitoring on hundreds of rows: 146 devices
admin-marked `offline` are observably `problem` (up, with a trigger); 4
admin-`online` are observably `down`; 30 admin-`online` read `unknown`.

The obvious fix — "show `live_status` instead" — is wrong, because a naive swap
trades one inaccuracy for three new ones (measured on live prod data):

1. **89 of 493 active devices have no `live_status` at all** (never warmed —
   not Zabbix-reconciled; 72 are CPE-role whose truth is ACS/OLT-poll, not
   Zabbix). They'd render blank.
2. **~162 "unreachable" devices have no monitoring path** (their IP isn't routed
   by the one live WireGuard tunnel). `live_status=down` there means "we can't
   see it," not "it's offline." A swap lights up ~160 false red alarms.
3. **Maintenance/decommissioned intent is lost** — a deliberately disabled
   device would flip to "down" and start alarming.

## 2. Strategy — three separate concepts

Keep these distinct; never collapse them into one field.

| Concept | Meaning | Examples |
|---------|---------|----------|
| `device.status` | **administrative / lifecycle intent** | active, inactive, maintenance, (decommissioned) |
| `live_status` | **raw monitoring observation** | up, down, problem, unknown |
| **`operational_status`** | **derived, UI-facing NOC truth** (a *projection*, not a stored column) | up, degraded, down, unmonitored, maintenance, unknown |

`operational_status` is a pure function of:
`lifecycle intent + monitoring coverage + live state + freshness + trigger severity`.

### Precedence ladder (first match wins)
```
if admin intent is maintenance / decommissioned / retired:  -> that (intentional; never alarm)
elif no monitoring target / no monitoring path:             -> unmonitored   (reason: no_path / no_target)
elif no live_status row:                                    -> unmonitored   (reason: not_warmed)
elif warmer heartbeat is stale:                             -> unmonitored   (reason: stale)
elif live_status == unknown:                               -> unmonitored   (reason: monitoring_unknown)
elif live_status == problem:                               -> degraded
elif live_status == down:                                  -> down
elif live_status == up:                                    -> up
else:                                                       -> unknown
```

Key rule: **never alarm on `unmonitored`.** Alarm only on `monitored + down/degraded`.
This is the line that lets the NOC use live truth without turning monitoring
gaps into fake outages.

## 3. Non-negotiable refinements (from how this lands in the codebase)

1. **Observation is per device type.** The authoritative live source differs:
   NAS/router → Zabbix (+RADIUS sessions); OLT → OLT polling
   (`last_ping_ok`/`last_poll_status`) + Zabbix; ONT/CPE → ACS
   (`acs_last_inform_at`) + OLT `olt_status`. The 72 not-warmed CPEs are
   *monitored* — just not by Zabbix. The deriver must select source(s) by type,
   resolving "reachable if **any** authoritative source confirms recently."
   *(Phase 1 ships the Zabbix path; per-type ACS/OLT sources land Phase 2.)*
2. **`monitoring_path` is a cached signal, not a per-row computation.** A job
   materializes a reachable-CIDR set (up-tunnel `wg` allowed-IPs + routes) and
   caches it; the deriver does a cheap IP-membership check. This is the
   highest-effort piece and is also the VPN-restoration worklist input.
   *(Phase 3.)*
3. **`operational_status` is a projection — do NOT persist a 4th column.**
   Compute on read (cache the result if render cost shows up). There are already
   three status-ish fields; a durable fourth just becomes the next thing to
   reconcile.
4. **Share the coverage predicate with the SLA bridge.** The availability →
   uptime-alert bridge (already live) must not log downtime for
   `unmonitored`/`stale` devices, or the SLA numbers inherit the same blind-spot
   pollution. One coverage predicate, used by this page *and* the bridge.
5. **Render ~5 pills, not 8 states.** UI buckets: **Up / Degraded / Down /
   Unmonitored / Maintenance** (+ Unknown). The fine-grained reason
   (`no_path`, `stale`, `not_warmed`, `monitoring_unknown`) lives in the tooltip
   and the filter values, not as competing pills.

## 4. UI

- **Primary pill:** derived `operational_status`.
- **Secondary text:** admin status, shown only when it differs.
- **Tooltip:** source + reason ("Zabbix unreachable", "No monitoring path",
  "Admin: maintenance", "Warmer stale").
- **Filters:** Operational status · Admin status · Coverage (monitored /
  unmonitored) · Status mismatch.
- **Mismatch worklist** (inventory-hygiene engine; each reason routes to an owner):
  - `admin_online + observed down/problem`  → field ops
  - `admin_offline + observed up/problem`   → inventory hygiene
  - `active + unmonitored`                  → net-eng / VPN
  - `monitored + stale`                     → monitoring infra
  - `monitored + down >30d + 0 customers`   → decommission candidate (the 171 dead Zabbix hosts)

## 5. Phasing

1. **Phase 1 (this PR — read-only, no schema):** `derive_operational_status()`
   pure function (lifecycle override + not_warmed/stale/unknown → unmonitored +
   live mapping + mismatch flag), reusing the warmer-heartbeat staleness reader.
   Network Devices page renders the derived pill primary, admin secondary,
   reason tooltip. Tests on the pure function.
2. **Phase 2:** per-type observation (ACS/OLT-poll/RADIUS), filters + mismatch
   worklist, one-reader rollout to device API + monitoring KPIs + the
   performance wallboard.
3. **Phase 3:** cached reachable-CIDR coverage job (real `no_path`
   distinction); gate the SLA bridge with the shared coverage predicate; daily
   mismatch-by-reason report.

## 6. Key files
| Thing | Path |
|-------|------|
| Network Devices page route | `app/web/admin/network_core_devices.py:64` |
| Page data builder (`core_devices`) | `app/services/web_network_core_devices_views.py:2951` |
| Template status pill | `templates/admin/network/network-devices/index.html:187` |
| Warmer heartbeat staleness (reuse) | `app/services/topology/selfcare.py:40` (`_warm_is_stale`) |
| live_status warmer + heartbeat key | `app/services/topology/live_status.py` |
| SLA bridge to gate (Phase 3) | `app/services/topology/availability_log.py` |
| Models (`status`, `live_status`) | `app/models/network_monitoring.py` |
