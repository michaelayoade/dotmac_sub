# Outage Classifier & Last-Mile Diagnoser

Turn three monitoring signals into a single, self-consistent judgement: **what is
actually down, how deep, and why** — and never mistake one customer's dead router
for a basestation outage.

## 0. Principle

Signals are not peers you AND/OR together — they form a **dependency ladder**, and
that ordering makes them validate each other. **Localize by proof-of-life,
classify by signal agreement, never override a contradiction — repair it.**

All inputs already exist on prod (nothing new to collect):

| Signal | Source | Layer |
|---|---|---|
| session (live customer) | `radius_active_sessions` (reconciled from `radacct`) | data plane |
| snmp (agent/interfaces) | OLT/MikroTik polling | mgmt plane |
| ping / icmp / interface-avail | Zabbix warmer → `network_devices.live_status` (+ `uisp.status`, #850) | reachability |
| ONT state + optical Rx | OLT → `ont_units.olt_status`, `onu_rx_signal_dbm` | fiber CPE |
| radio assoc + RF signal | UISP station data | wireless CPE |
| router last-inform | GenieACS → `acs_last_inform_at` | customer router |
| auth reject / no-attempt | `radius_error` / radacct | customer auth |

## 1. The node ladder (per pingable device: OLT, NAS, AP, BTS-router)

```
session  ── highest proof of "actually serving"
  ⇑ requires
snmp
  ⇑ requires
ping     ── lowest / reachability
```
`session UP ⟹ snmp & ping MUST be up`. `ping UP ⇏ session up`.
So **`session up + ping down` is physically impossible** → the lower signal is
lying (broken check / mgmt-path / stale), NOT the device.

| ping | snmp | session | class | action |
|---|---|---|---|---|
| up | up | up | healthy | — |
| up | up | down | **service / data-plane** | fix PPPoE/RADIUS/upstream; NOT "area down" |
| down | up/dn | up | **monitoring_fault:ping** (impossible) | self-heal the check; never "down" |
| up | down | up | **monitoring_fault:snmp** (impossible) | self-heal creds/agent |
| down | down | down | **node_outage** | notify + dispatch |

**Down requires all three consistently dark.** Contradictions emit a typed
self-heal/alert event (`monitoring_fault:*`, `service_fault:dataplane`) — the
higher layer's truth flows down to name which lower layer failed. No hard-coded
"session wins" override; the discrepancy drives the layers back into agreement.

## 2. Proof-of-life aggregation (per shared element, active OR passive)

For any element E with downstream customers D(E):
- `online(E)` = customers in D(E) with a **fresh** live session.
- `baseline(E)` = how many are **normally** online at this hour/day (temporal, per-element, from session history).

> **`online(E) ≥ 1` ⟹ E and everything upstream is UP.** One survivor vetoes "down".

E is a **suspect** only when `online(E)` collapses **well below `baseline(E)`**
(NOT "== 0" — see edge case 2) while a **peer/parent still has survivors**.

## 3. Localization = deepest dark-under-live

1. Group currently-down customers by shared element at each tier
   (CPE → AP/sub-split → PON/splitter → OLT/BTS → NAS → agg → core).
2. Find the **deepest** element whose `online` collapsed below baseline **but whose
   parent still has survivors** — that is the failure boundary.
3. Cross-check that element's device ladder (if pingable) to name the class.

Confidence = f(N, baseline-deviation, signal-agreement). Degrades gracefully to
per-customer last-mile when N is small or signals conflict.

## 4. Fiber tiers (what is auto-observable vs not)

A **PON port lives on the OLT** and a **PON = splitter (1:1)** — fully pollable
(per-ONT status + Rx). Only the splitter's internal **sub-split branch / splice /
leg** is dark (passive glass; manual `SplitterPortAssignment`/splice records rot).

| Tier | Auto? | From |
|---|---|---|
| ONT (one customer) | ✅ | OLT: ONT online + Rx |
| **sub-split branch / splice** (subset of a PON) | ❌ (infer — §6) | manual records only |
| PON port = splitter (whole PON) | ✅ | OLT: whole-PON ONTs dark / PON oper-state |
| OLT | ✅ | device / uplink |

A subset-of-PON fault is still **detected & classified** ("physical fault on a
shared branch of PON-X, these N customers") from the OLT alone — survivors prove
the PON/feeder up, the cluster proves it's shared — you just can't name the splice.

**Wireless localizes only to BTS/NAS** (which AP/sector a radio is on isn't
reliably known — the P2 gap). The asymmetry is real: fiber → inferred splitter
branch; wireless → basestation.

## 5. Last-mile diagnoser (below session, per session-down customer)

```
session (PPPoE)  ← symptom
  ⇑ CPE authenticating   ← RADIUS reject + ACS last-inform
  ⇑ CPE link healthy     ← optical Rx (fiber) / RF (wireless)
  ⇑ CPE present at node  ← ONT registered on OLT / radio associated on AP
  ⇑ CPE powered          ← absent everywhere ⟹ off
```

| Signature (session down, infra up) | Verdict | Customer/agent message |
|---|---|---|
| ONT/radio absent | `power` (off / drop cut) | "check ONT has power/lights" |
| present, bad signal | `signal_degraded` | schedule tech; not "area down" |
| present, good signal, ACS stale | `router_offline` | "reboot your router" |
| present, good signal, ACS informing + RADIUS reject | `auth` | operator-fix, no truck roll |
| present, good signal, no RADIUS attempt | `config` | not dialing |

## 6. Splice inference (recover the unpollable branch — §4)

Derive the sub-PON topology you can't poll, from OLT per-ONT telemetry over time:
1. **Co-failure clustering** — ONTs that repeatedly go dark *together* share a branch; failure history reconstructs the sub-PON tree.
2. **Correlated Rx shifts (predictive)** — a bend/failing splice upstream of a branch attenuates every ONT beyond it by the **same dB**. A cluster of matching Rx droop = a dying branch, caught **before** it cuts.

Reconcile the inferred grouping against `SplitterPortAssignment` (diff-not-mirror);
the inference *becomes* the plant map that was never maintained and flags where the
records disagree with reality.

## 7. Edge cases (each with its handling)

| # | Trap | Handling |
|---|---|---|
| 1 | Small-N element (no survivors possible) | below N-threshold, don't infer plant outage — fall to per-customer last-mile |
| 2 | Denominator (dormant/churned, diurnal offline) | `baseline(E)` temporal per-element; trigger on deviation, not `== 0` |
| 3 | Customer-area power cut vs plant fault | ONTs OFF + OLT/PON healthy ⟹ customer-side power, NOT your plant, no dispatch |
| 4 | Failover / roaming (online elsewhere) | proof-of-life is GLOBAL — before flagging a node down, check its droppers online anywhere; if yes → failover, zero customer impact |
| 5 | Session on NAS, fault upstream of NAS | NAS ping/snmp cross-check + regroup by access element (one OLT's customers only → OLT/backhaul, not NAS) |
| 6 | Staleness / eventual consistency | count session as proof only if last-update fresh; require collapse to persist across a debounce window |
| 7 | Degraded ≠ down (flapping) | separate class from session churn (reconnect rate) + marginal Rx/RF |
| 8 | Maintenance | maintenance-window input suppresses declaration; customer banner = "planned work" |
| 9 | Mixed-medium DAG (NAS shared fiber+wireless) | localize on the DAG: both-medium collapse → shared node; single-medium → that access tier |
| 10 | Localization depth by medium | fiber → PON/inferred-branch; wireless → BTS. State the precision. |

## 8. Implementation phases

- **P1 — core classifier**: `online_count`/`baseline` per node in `affected_customers`; the per-node ladder state (§1); localization (§3). Feeds the outage console + impact page. *(uses only deployed data)*
- **P2 — last-mile diagnoser** (§5): per session-down customer verdict; feeds support view + selfcare "what's wrong" + per-customer-vs-area notification split.
- **P3 — splice inference** (§6): co-failure + correlated-Rx branch inference; predictive-maintenance alerts.
- **P4 — surfaces**: admin outage console using the classifier; selfcare/mobile connection status; notification send-path (gated on comms policy, not data).

Every phase consumes signals already on prod. The build is the **correlation engine
+ the temporal baseline** — no new collection.
