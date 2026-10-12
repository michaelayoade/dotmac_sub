# Router health reconciler (`network.router_health`)

Read-only. The reconciler never writes to a router and never changes the policy.
It judges stored state and the daily CoA canary, and projects findings into the
alert console (`/admin/alerts?category=network`). Every control ships **off** and
findings ship in **shadow** mode (visible, no notifications).

Scope of this document: layer 1 (snapshot freshness and config posture) and
layer 3 (CoA service path, canary). Layer 2 (runtime REST polling, the routers
list and detail "Health" UI, the drift page "Router posture" section and optical
power checks) is PR2. Optical-power checks were approved by Michael on
2026-10-12 and are deliberately not part of PR1.

## Controls and settings

| Key | Default | Purpose |
|---|---|---|
| control `network.router_health_posture` | off | posture + service-path reconciliation (database only) |
| control `network.router_health_coa_canary` | off | sends no-op CoA probes to production NAS |
| setting `network_monitoring.router_health_alert_mode` | `shadow` | `shadow` = no notifications; `alert` = notify on new/reopened/escalated findings. Owner-command only (audited) |
| setting `network.router_health_policy` | `{"version":1}` | roles, overrides, dated exceptions. Owner-command only (audited) |
| `router_health_snapshot_max_age_hours` / `_high_age_hours` | 36 / 168 | stale = medium, then high |
| `router_health_coa_window_hours` / `_timeout_min_count` / `_timeout_ratio_pct` | 72 / 3 / 20 | `coa-timeouts` |
| `router_health_enforcement_failure_min_count` | 3 | `enforcement-path-failing` |
| `router_health_coa_canary_*` | 24 h / 20 h / 50 / 2000 ms / 300 s | interval, per-NAS minimum spacing, max targets, packet spacing, re-probe delay |
| `router_health_max_findings_per_run` | 300 | overflow produces one `policy-overflow` finding |

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

## Rollback

Disable the controls. To clear the alerts run
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
