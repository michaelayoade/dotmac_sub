# Router health reconciler (`network.router_health`)

Read-only. The reconciler never writes to a router and never changes the policy.
It judges stored state and the daily CoA canary, and projects findings into the
alert console (`/admin/alerts?category=network`). Every control ships **off** and
findings ship in **shadow** mode (visible, no notifications).

Scope of this document: layer 1 (snapshot freshness and config posture),
layer 2 (runtime REST poll, the routers list and detail "Health" UI, the drift
page "Router posture" section and optical power checks) and layer 3 (CoA service
path, canary). Optical-power checks were approved by Michael on 2026-10-12;
they ship with their own control, **off**, so rollout can turn them on
deliberately.

## Controls and settings

| Key | Default | Purpose |
|---|---|---|
| control `network.router_health_posture` | off | posture + service-path reconciliation (database only) |
| control `network.router_health_coa_canary` | off | sends no-op CoA probes to production NAS |
| control `network.router_health_runtime_poll` | off | periodic read-only REST poll of every active router (layer 2) |
| control `network.router_health_optics` | off | SFP optical power checks; needs the runtime poll and one read-only POST per router per hour |
| setting `network_monitoring.router_health_alert_mode` | `shadow` | `shadow` = no notifications; `alert` = notify on new/reopened/escalated findings. Owner-command only (audited) |
| setting `network.router_health_policy` | `{"version":1}` | roles, overrides, dated exceptions. Owner-command only (audited) |
| `router_health_snapshot_max_age_hours` / `_high_age_hours` | 36 / 168 | stale = medium, then high |
| `router_health_coa_window_hours` / `_timeout_min_count` / `_timeout_ratio_pct` | 72 / 3 / 20 | `coa-timeouts` |
| `router_health_enforcement_failure_min_count` | 3 | `enforcement-path-failing` |
| `router_health_coa_canary_*` | 24 h / 20 h / 50 / 2000 ms / 300 s | interval, per-NAS minimum spacing, max targets, packet spacing, re-probe delay |
| `router_health_max_findings_per_run` | 300 | overflow produces one `policy-overflow` finding |
| `router_health_runtime_interval_minutes` | 15 (10-60) | runtime poll interval |
| `router_health_runtime_concurrency` / `_connect_timeout_seconds` / `_read_timeout_seconds` / `_router_deadline_seconds` | 6 / 5 / 10 / 45 | bounded concurrency, per-request timeouts and the total time allowed per router |
| `router_health_unreachable_polls` | 2 | consecutive failed polls before `rest-unreachable` / `rest-tls` |
| `router_health_ntp_grace_minutes` / `_clock_skew_seconds` / `_clock_skew_high_seconds` | 30 / 60 / 3600 | `ntp-unsynced` grace, `clock-skew` medium and high thresholds |
| `router_health_bgp_grace_minutes` / `_ospf_grace_minutes` | 30 / 30 | grace before `bgp-session-down` / `ospf-neighbor-lost` |
| `router_health_link_downs_24h_warn` / `_high` | 20 / 200 | `link-flapping` medium / high |
| `router_health_sfp_rx_low_dbm` / `_rx_critical_dbm` / `_overload_dbm` | -22 / -25 / 2 | optics: below low = medium, below critical = high, above overload = high |
| `router_health_ppp_drop_pct` / `_ppp_min_baseline` | 50 / 10 | `ppp-drop` |

Known limitation: findings already open when the mode flips from `shadow` to
`alert` do not notify; only new, reopened or escalated findings do. Review the
open list at the flip.

## Findings

Fingerprint: `network:router-health:{layer}:{subject-uuid}:{check}:{qualifier}`.
Priority maps to alert severity: critical -> critical, **high -> warning**
(`details.priority` keeps `high`), medium -> warning, low -> info.

Snapshot layer: `snapshot-never` (high), `snapshot-stale` (medium, high when very
old), `export-host-key-changed` (high), `snapshot-unparseable` (low).
Posture layer: `input-v4-missing`, `input-v4-bypass`, `input-v6-missing`,
`snmp-exposed` (critical if a community is writable), `reverse-proxy-enabled`,
`ntp-client-off`, `radius-incoming-off`, `suspension-jump` and `suspension-chain`
(critical), `suspension-shadowed`, `splynx-legacy-chain`, `splynx-legacy-list`.
Service layer: `coa-timeouts`, `enforcement-path-failing`, `coa-canary-timeout`,
`coa-canary-acked` (critical), `coa-canary-config`, `coa-canary-stale`.
Policy layer: `policy-invalid`, `policy-unmatched`, `policy-exception-expired`,
`policy-overflow`.

Evidence is allowlisted structured fields only. SNMP community **names** are
never emitted (communities are reported by index). A parse coverage below 98 %,
or a missing `/system identity`, means posture is **not** judged for that
router: prior posture findings are carried forward and `snapshot-unparseable`
is raised. Resolution is scoped per (layer, subject, check), so one failing
router never clears another's findings.

## Snapshot scope widening and SSH host keys

`router_sync.capture_scheduled_snapshots` now attempts every active router that
is not in `maintenance` (it used to skip anything not `online`, so a router stuck
at a non-online status was never snapshotted and never reported). Each outcome
is stored as a failure **class** only (`auth_failed`, `unreachable`,
`host_key_changed`, `host_key_unknown`, `empty_export`, `no_auth_configured`,
`other`), never exception text.

Routers that were not `online` are contacted with **strict host-key checking**:
a host whose key is not already in `router_config_ssh_known_hosts_path` is
refused (`host_key_unknown`) instead of being pinned on first use. To bring such
a router into snapshots, pre-seed its host key from a trusted path
(`ssh-keyscan` from the management host, verify the fingerprint out of band),
then it succeeds on the next run. Routers that are `online` keep the existing
behaviour (a changed key is always refused).

## Policy

`docs/network/router_health_policy.example.json` is a synthetic example (placeholder router names, dates and addresses). The real starting policy is kept outside the repository (private cutover notes).
It uses documentation-range addresses only. The real policy (real router
names, exception scopes and dates) is kept in the private cutover notes and is
applied through the audited settings write; never commit it or real addresses
here. A `router_name` match that finds no router is reported as
`policy-unmatched` (low).

Rules: every override and exception needs a reason; overrides that weaken a
check need `until` (at most 180 days ahead) and lapse at the end of that day
(Africa/Lagos), after which the check evaluates normally and
`policy-exception-expired` names the lapse for 7 days. `annotations` never
suppress anything, they add `known_issue` text. A router with no entry gets the
strictest defaults.

## CoA outcomes and the canary

`enforcement.py` records the disposition of every CoA attempt in
`nas_coa_outcome_buckets` through `network.coa_outcome_evidence` (out of band,
ADR 0018). The canary sends a Disconnect-Request carrying only a synthetic,
unique `User-Name` (`dotmac-coa-canary-<hex>`) that is checked against
credentials and RADIUS users first. Healthy replies are NAKs; an ACK is
critical. It uses a separate exchange function, so it neither reads nor writes
the CoA negative cache. The `started` audit event is committed before the first
packet. Results are attributed to every active NAS row on the same host and CoA
port. A timeout is re-probed once; only a repeated failure alerts.

Each canary packet adds one request and one NAK to the router's RADIUS
counters; do not read those as bad requests.

## Layer 2: runtime poll

Every `router_health_runtime_interval_minutes` the poll reads each active,
non-maintenance router that has REST credentials, with bounded concurrency and a
per-router deadline. It uses `RouterConnectionService.execute_once` with a fixed
allowlist of nine `GET` paths (a test pins this) and stops a router at the first
auth or transport failure. It never reads PPP usernames (`/ppp/active` count
only) and never writes `Router.status` or `last_seen_at`.

| Check | Priority | Meaning |
|---|---|---|
| `rest-auth-failed` | high | HTTP 401: Sub's REST login is rejected or missing on the router |
| `rest-forbidden` | medium | HTTP 403: authenticated but the user lacks the read/api policy |
| `rest-unreachable` / `rest-tls` | high (medium if the router had no sessions at last poll) / medium | timeout, refused, or TLS failure for `router_health_unreachable_polls` consecutive polls |
| `ntp-unsynced` | medium | NTP client disabled or not `synchronized` for longer than the grace |
| `clock-skew` | medium, high above the high threshold | router UTC differs from server UTC (ISO and legacy `mon/dd/yyyy` dates, GMT offset applied) |
| `bgp-session-down` | high | an enabled BGP connection is not established beyond the grace; policy exceptions (check `bgp-session-down`, scope = peer address) suppress |
| `ospf-neighbor-lost` | high | a neighbour seen Full at least 4 times is not Full beyond the grace; forgotten after 7 days absent |
| `link-flapping` | medium / high | physical interface link-down counter delta over 24 h; a reboot or counter reset restarts the baseline |
| `ppp-drop` | high | active PPPoE count below the baseline (median of samples 30-150 min ago) by the configured share and by at least 10, on two consecutive polls. A sustained drop stops being flagged once the baseline window rolls past it (about 2.5 h) |
| `ops-user` | high / medium | the snapshot login (policy `ops_user`) is missing, blocks the sub-prod address, or is in a different group than expected (full instead of read) |
| `rest-user-missing` / `rest-user-full` | high / medium | the REST login (policy `rest_user`) is not in the router's user list, or is in a forbidden group |
| `full-users-unrestricted` | low | users in group `full` with no address limit (names listed, no passwords) |
| `router-rebooted` | low | uptime decreased; auto-resolves after 24 h |

A router that cannot be read judges only the four access checks; every other
runtime finding is carried forward untouched. A check whose endpoint is
unsupported (for example BGP on a router without the routing package) is
likewise not judged.

Assumptions to confirm from the first real polls (marked [I] in the design):
the RouterOS 7 `status` field values of `/system/ntp/client` (`synchronized`),
the `/system/clock` field names, and the BGP `remote.address` / `established`
representations. When `status` is absent only `enabled` is judged.

### Optical power (`network.router_health_optics`, off)

RouterOS has no GET for SFP diagnostics. With the control on, once per router
per hour (55 minute minimum), Sub sends `POST /interface/ethernet/monitor`
with `numbers` set to that router's SFP/QSFP interface names (at most 16, found
from `/interface`) and `once`. This samples the module and changes nothing.
`sfp-rx-low` (medium below `router_health_sfp_rx_low_dbm`, high below the
critical level, high for dark fibre: -40 dBm or lower with the link down) and
`sfp-overload` (high above `router_health_sfp_overload_dbm`) are judged only
from those readings. Turning the control off resolves the optics findings.
Michael approved enabling optics on 2026-10-12; it is a rollout step, not a
default.

## Rollback

Disable the controls (`network.router_health_runtime_poll` and `network.router_health_optics` stop the poll and the POST immediately; PR2 has no migration). To clear the alerts run
`python scripts/router_health_retire_findings.py` (dry run by default; `--apply`
resolves every `network:router-health:` alert and writes an audit event).
Migration 672 is inert and may stay; `downgrade()` drops the evidence tables.

## Suspension-path bypass checks

Beyond "the jump exists and the chain ends in a drop", posture now reports
(`suspension-jump` / `suspension-chain`, critical unless noted):

- a `return`, or any `accept` other than the narrow DNS (`udp` 53) and portal
  (`tcp` 80/443 to a single host) allows, placed before the terminal
  reject/drop in a suspension chain (rule order is evaluated);
- a forward jump narrowed by matchers beyond `src-address-list=<suspended>` and
  a negated `dst-address-list` (`narrowed`);
- the negated walled-garden destination list holding prefixes shorter than
  `suspension_exclusion_max_prefix_breadth` (default `/24`) (critical) or more
  than `suspension_exclusion_max_entries` entries (default 50, high).

Policy `defaults` accept `suspension_portal_addresses` (single IPs). When it is
empty, any single-host `tcp` 80/443 allow counts as the portal allow; set it to
pin the allowed portal address exactly.

### Fresh-schema default parity

Revision 001 bootstraps the current ORM schema, and the shared migration schema
operations owner skips already-existing objects during subsequent revisions.
The ORM therefore declares the same server defaults as published revision 672:
`router_health_signals.samples = []`, `consecutive_abnormal = 0`, and
`nas_coa_outcome_buckets.count = 0`. Python defaults remain in place. This
alignment changes fresh schema construction; it does not rewrite published
migration bodies or repair arbitrary pre-existing schema drift. Native SQL
canaries in `tests/integration/test_router_health_insert_defaults.py` check
actual migrated defaults and preserve an explicitly supplied counter value.
