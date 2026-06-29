# Networking modules — UX-polish & operator-control audit

**Date:** 2026-06-29
**Method:** 7-agent parallel read-only review across the networking admin surface
(~33 admin pages, ~50 services): router-management, RADIUS/enforcement,
OLT/ONT/PON, TR-069/CPE, topology/monitoring, IPAM/fiber/sites, WireGuard/DNS/speed.
**Status:** audit only — nothing implemented from this doc yet. Companion to
[BILLING_UX_POLISH_AUDIT.md](BILLING_UX_POLISH_AUDIT.md).

> Note: a separate PR (#505) already landed adjacent router/MikroTik hardening
> (REST-path config-push syntax, enqueue-failure handling, principal audit actor,
> post-failure snapshots, snapshot scoping, route reordering, profile-change +
> address-list API fallbacks, idempotent static-suspend). Findings here build on
> that and exclude it.

## What this audit is

Two separate tracks, kept apart because they carry different risk and review needs:

- **POLISH** — make existing, working features *feel finished*: complete states
  (loading/empty/error/partial-success), clear feedback/affordances, accurate
  labels & freshness signals, surfaced capability, consistency, a11y. No new
  capability, no logic change.
- **CONTROL** — hand the operator a knob the code currently hardcodes: a setting,
  a per-action option, a safety mode, or an override. This is *small-feature*
  work (persistence + validation + RBAC), surfaced by the polish audit but scoped
  and reviewed as features.

When a hardcoded value is found, decide with four questions: (1) would a real
operator reasonably want it different per-tenant/site/case? (2) what is the safe
default + valid range? (3) who may change it? (4) is it a setting (persisted), a
per-action option, or an env/feature flag?

## Acceptance criteria (the bar a "polished" networking page must meet)

1. Every mutating action has a visible in-flight state (disable + spinner), a
   success/error result the operator can see, and a confirm if it's
   disruptive/destructive.
2. Bulk actions report **partial success** ("N of M; failed on X").
3. Every "live"/dashboard view shows **data-as-of** and either truly
   auto-refreshes or doesn't claim "Live".
4. Labels/legends/units match what is actually shown; timezones are correct.
5. No backend filter/field/endpoint is unreachable from the UI.
6. Load-bearing constants have a single source of truth (no duplicated
   "keep in sync" literals).

**Reference exemplar to copy:** IPAM already meets this bar — release confirms with
audit-log copy, bulk-assign per-row success/error tables, content-aware CIDR
validation (`app/services/web_network_ip.py:1797`).

## Cross-cutting themes

### POLISH

**P-A. Destructive/disruptive device actions fire with no confirm.** High blast
radius, cheap fix. Standard: confirm dialog naming the action + target count.
- ONT soft-reboot / OLT reboot (`templates/admin/network/onts/_quick_actions.html:71-92`)
- CPE reboot + **bulk Reboot-All / Factory-Reset-All** (`templates/admin/network/tr069/index.html:158-175`, `cpes/_tr069_partial.html:19`)
- VPN **Regenerate Keys** + **Stop/Restart** (`templates/admin/network/vpn/index.html:208,258-264`)
- NAS "Generate setup script" which **rotates live API creds** (`templates/admin/network/nas/device_detail.html:528`)
- Fleet config-push (no confirm modal); Import-PPPoE-credentials.

**P-B. Async actions with no loading/result feedback** (single biggest UX uplift).
Standard: spinner + disable-on-submit + re-render-or-toast with result; partial-
success for bulk.
- CPE panel uses `hx-swap="none"` so values stay stale after "Refresh"; port-toggle
  never reflects new state (`cpes/_tr069_partial.html:9-23`)
- ONT "Provision" navigates to a **raw JSON dump** (`onts/provision.html:134`)
- Router Sync/Test no spinner/toast (`routers/detail.html:35`)
- Service-port create/delete, NAS ping/backup/test-API, IP bulk ops, fiber
  approve/reject — all missing busy state
- Bulk service-port clone aborts on first failure, no "N of M" (`app/services/web_network_service_ports.py:624`)

**P-C. Misleading "live"/"trend"/freshness signals.** Standard: accurate labels,
"as of <ts>", real auto-refresh or relabel, stale badges, complete legends/units.
- Monitoring "Live ·" badge is manual-refresh-only (`monitoring/_kpi_partial.html:8`)
- "ONU Status Trend (24h)" is a **single point** (`monitoring/index.html:187`)
- "Last updated … Africa/Lagos" shows a **UTC value** (off by +1h) (`monitoring/index.html:36`)
- "Live RADIUS active sessions" is a static snapshot (`sessions.html:22`)
- Topology **node-color legend missing** (`topology/index.html:77`)
- PON status has no last-seen / stale badge

**P-D. Half-exposed capability** — backend supports it, UI hides it. Standard:
surface it.
- `nas_filter` (sessions), speedtest `date_to`, DNS-threat device filter
- `RadiusProfile.mikrotik_address_list` has no form field (`app/services/web_network_radius.py:800`)
- Decommission reason/checkboxes hardcoded (`onts/_decommission_modal.html:66`)
- Firmware push is free-text URL with no catalog dropdown (`tr069/index.html:558`)
- Orphan endpoints with no button: CPE factory-reset, router test-connection /
  capture-snapshot

**P-E. Pagination / empty / loading gaps.**
- Routers pagination broken — page-size 25 in template vs `limit=50` backend, fleet
  silently capped at 50 (`routers/index.html:137`)
- Fiber/FDH hard-capped at 200 rows, no pager (`app/services/web_network_fiber.py:107`)
- Leaflet map: no loading/empty/tile-error state

### CONTROL

**C-1. Operational thresholds hardcoded → settings** (safe defaults = today's
values, NOC-lead editable):
- optical dBm good/warn `-25/-28` (`app/services/zabbix_ont_status.py:65`)
- link-utilization `90/70/40` (`app/services/network_topology.py:423`)
- device-health CPU/mem/temp `85/60 · 90/70 · 70/55` (`monitoring/index.html:309`)
- stale-open-incident `36h` (`app/services/topology/outage.py:27`)
- warm-stale `600s` **duplicated** in two files (`app/services/device_operational_status.py:42` + `app/services/topology/selfcare.py:37`)
- speedtest SLA ratio `0.8` (`app/services/web_network_speedtests.py:692`)

**C-2. Deployment-specific identifiers baked in source → setting/lookup table:**
- ⚠️ **Address-list name drift (correctness-adjacent):** suspend/block list is
  `"suspended"` in populate + reconciliation sweeps (`app/services/radius_population.py:44`,
  `radius_reconciliation.py:49`), `"blocked-subscribers"` in the static-suspend
  path, and configurable `default_mikrotik_address_list` in enforcement — 3+
  readers/writers, 2 literals. Drift → suspension audit reports false `open_access`
  and rules won't match populate. → one `radius.suspended_address_list` setting.
- internet **VLAN `203`** classifies every service port (`app/services/web_network_service_ports.py:468,622`)
- tunnel-pubkey→site and mgmt-subnet→site maps in source (`app/services/web_network_monitoring.py:24-28`)
- captive allow-ports + block-chain name (`app/services/radius_reject.py:362`)
- bootstrap user/ports `dotmacapi/8728/443` (`app/web/admin/nas.py:671`)
- core-device backup **SSH port default `120`** — fails most gear (`app/web/admin/network_core_devices.py:716`)
- FDH splitter defaults `1:8` (`app/services/web_network_fdh.py:319`)

**C-3. Timeouts/retries/TTLs → settings or per-router override:** connection
`CONNECT/READ/MAX_RETRIES` (`app/services/router_management/connection.py:23`),
acct-interim `300` (duplicated), ACS test timeout `5s`, WG token TTL `24h` /
keepalive `25` / log-retention `90d`, ping packet count `4`.

**C-4. Per-action options / safety modes (new operator control — highest
"give me control" value):** config-push **dry-run / preview-diff**; config-push
**on_failure: continue│abort│rollback** (pre-snapshot already captured);
admin-editable `DANGEROUS_COMMANDS` + per-push override; firmware **catalog
dropdown** vs free-text; **per-session Disconnect** on "Who's Online"
(`sessions.html`); decommission reason/options; ping count; router-API
**TLS-verify** toggle.

## Priority

| Tier | Items |
|------|-------|
| **P0** | Unify suspend/block address-list name (C-2); internet-VLAN 203 → setting (C-2); core-device SSH port 120 → 22 (C-2); add confirms to destructive device actions (P-A) |
| **P1** | Async-action feedback standard (P-B); fix live/trend/tz labels + freshness (P-C); surface hidden capability (P-D); thresholds → settings (C-1); config-push dry-run + on_failure + editable blocklist (C-4); routers pagination (P-E) |
| **P2** | timeouts/TTLs (C-3), fiber/FDH pagination, map states, graph/zoom defaults, retention windows, bootstrap/captive ports, auth-preset seeding |

## Suggested slicing

- **Slice 1 (P0):** address-list unification (folds into the #505 line of work that
  touched static-suspend), VLAN/SSH-port settings, destructive-action confirms.
- **Slice 2:** the async-feedback + freshness polish standard, page-by-page,
  starting with device-touching pages (CPE, ONT, routers, NAS, VPN).
- **Slice 3:** config-push operator controls (dry-run / on_failure / editable
  blocklist) — its own feature PR with acceptance tests.
- **Slice 4:** threshold settings + hidden-capability surfacing.

## Appendix — full findings by cluster

Format: `[POLISH|CONTROL] (severity) file:line — problem → recommendation [recommend|defer]`

### Router management
- [POLISH] (High) `templates/admin/network/routers/detail.html:35` (and `jump_hosts.html:84`) — "Sync Now"/"Test" use `hx-post hx-swap="none"`: no spinner/toast/status; sync can take seconds or 502 unseen → swap to target/toast with loading state [recommend]
- [POLISH] (High) `templates/admin/network/routers/index.html:137-154` — pagination reads a `pagination` object `list_context` never builds, hardcodes page-size 25 vs backend `limit=50`; fleets capped at 50, no Next → build pagination context / reconcile page size [recommend]
- [POLISH] (High) `templates/admin/network/routers/push.html:34-41,132-137` — selecting a template dumps raw `{{var}}` into the textarea, sends no `variable_values`; `/config-templates/{id}/preview` unused → add variable inputs + render-preview before execute [recommend]
- [CONTROL] (High) `app/tasks/router_sync.py:139-211` — push always continues per-router on failure; pre/post snapshots captured but never used → per-action `on_failure: continue|abort|rollback` (default continue); rollback replays pre_change snapshot [recommend]
- [CONTROL] (High) `app/services/router_management/connection.py:14-21` — `DANGEROUS_COMMANDS` hardcoded → admin-editable setting (default = current list) + optional per-push override gated by `router:admin` [recommend]
- [CONTROL] (Med) `app/services/router_management/connection.py:23-26` — `CONNECT_TIMEOUT/READ_TIMEOUT/MAX_RETRIES/RETRY_BACKOFF_BASE` hardcoded → global setting (+ optional per-router override), default = current [recommend]
- [CONTROL] (Med) `app/api/router_management.py:310` + push.html — no dry-run/preview-diff → per-action `dry_run` (default off) rendering commands + pre-snapshot diff without writes [recommend]
- [POLISH] (Med) `templates/admin/network/routers/push.html:102-108` — fleet-wide push fires on one click; Step 3 is passive summary → confirm modal listing commands + target count [recommend]
- [POLISH] (Med) `templates/admin/network/routers/detail.html:167` — config-tab empty state claims snapshots captured on sync, but only scheduled task + pushes snapshot; no manual capture button though endpoint exists → fix copy + add capture button [recommend]
- [POLISH] (Med) `templates/admin/network/routers/detail.html` — no Test-Connection on detail page though endpoint exists; operators run full Sync (flips status to unreachable) just to check → add non-mutating Test with inline result [recommend]
- [CONTROL] (Med) `templates/admin/network/routers/form.html:97-105` — new routers default `use_ssl` on but `verify_tls` off silently → make secure default explicit / surface warning [recommend]
- [POLISH] (Low) `templates/admin/network/routers/push_detail.html:119` — auto-refresh is full `window.location.reload()` every 5s, loses state, loops if wedged → targeted fragment poll w/ max-attempts/stall notice [defer]

### RADIUS / enforcement / connection-provisioning
- [CONTROL] (High) `app/services/radius_population.py:44` + `radius_reconciliation.py:49` — walled-garden list hardcoded `"suspended"` in two sweeps while enforcement reads `default_mikrotik_address_list` and static-suspend writes `"blocked-subscribers"`; drift → false `open_access` + mismatched rules → one `radius.suspended_address_list` setting read by all [recommend]
- [CONTROL] (Med) `app/services/radius_population.py:43` + `connection_type_provisioning.py:546` — `ACCT_INTERIM_SECONDS=300` defined twice → single setting (range ~60–3600) [recommend]
- [CONTROL] (Med) `app/services/radius_reject.py:362-363` — `block_chain="dotmac-block-chain"` + `oss_ports="80,443,8101..8104"` hardcoded → captive allow-ports setting [defer]
- [CONTROL] (Med) `app/web/admin/nas.py:671-673` — bootstrap hardcodes `dotmacapi/8728/443`, ignores device's `mikrotik_api_port` → use device's port / expose as options [recommend]
- [CONTROL] (Low) `app/services/enforcement.py:89` (`_COA_NEG_TTL=15min`), `:444` (`burst_time or 10`), `radius_reconciliation.py:54` (`_OPEN_SESSION_WINDOW=2h`) → settings only if tuning needed [defer]
- [POLISH] (Med) `templates/admin/network/sessions.html:63` + `app/web/admin/network_radius.py:336` — "Who's Online" display-only; no per-session kick despite full CoA/API/SSH kick available → guarded Disconnect w/ confirm + result [recommend]
- [POLISH] (Med) `sessions.html:39` + `network_radius.py:338` — route accepts `nas_filter` but UI only has free-text box → add NAS select bound to `nas_filter` [recommend]
- [POLISH] (Med) `templates/admin/network/nas/device_detail.html:528` — "Generate setup script" rotates live API password with no confirm → add confirm [recommend]
- [POLISH] (Med) `nas/device_detail.html:105,268,522` — ping/backup/test-API buttons make blocking calls with no busy state → apply existing `x-data="{submitting}"` pattern [recommend]
- [POLISH] (Med) `templates/admin/network/radius/profile_form.html` + `web_network_radius.py:800` — `RadiusProfile.mikrotik_address_list` read as per-profile block list but no form input / not parsed → add the field [recommend]
- [POLISH] (Low) `sessions.html:22` — labelled "Live" but static snapshot → add "as of <ts>" (+ optional refresh) [defer]
- [POLISH] (Low) `templates/admin/network/radius/index.html:20` — "Import PPPoE Credentials" bulk write, no confirm/loading → add confirm + submitting state [defer]

### OLT / ONT / PON
- [POLISH] (High) `templates/admin/network/onts/_quick_actions.html:71-92` — Soft/OLT Reboot no `hx-confirm` (sibling connection-request IS) and no inline feedback → add confirm + result marker [recommend]
- [CONTROL] (High) `app/services/web_network_service_ports.py:468` (+ `:622`) — internet vs management classified by hardcoded `vlan_id in (203,)`; non-203 sites mis-classify every port → internet-VLAN setting (default {203}, 1-4094) [recommend]
- [POLISH] (High) `templates/admin/network/onts/provision.html:134` — "Provision ONT" plain POST → raw JSON page; no spinner/confirm; `async_execution` dropped → hx-post + indicator + result panel + confirm [recommend]
- [CONTROL] (High) `app/services/zabbix_ont_status.py:65` — optical cutoffs good ≥-25 / warn ≥-28 dBm hardcoded (dupes at :613,:657) → ONT_SIGNAL_GOOD/WARN_DBM settings (range ~-30..-8) [recommend]
- [POLISH] (High) `templates/admin/network/onts/_decommission_modal.html:66-69` — reason/deauthorize/remove-from-ACS hardcoded hidden inputs (reason always "hardware_fault") though route accepts them → reason select + 2 checkboxes [recommend]
- [POLISH] (High) `templates/admin/network/pon_interfaces/index.html:85` — Up/Down/Unknown shown as authoritative, no freshness; stale vs unreachable both "Unknown" → per-row last-updated/stale badge + page "data as of" [recommend]
- [POLISH] (Med) `onts/_decommission_modal.html:36` — only acts on status===200; partial decommission (ACS ok, OLT deauth failed) leaves modal open with no error → render step results inline on non-200 [recommend]
- [POLISH] (Med) `web_network_service_ports.py:624` — bulk clone sequential, aborts on first failure, generic message → "Created N of M, failed on VLAN X" [recommend]
- [POLISH] (Med) `templates/admin/network/onts/_service_ports_tab.html:57` (and :37) — create/delete OLT-SSH forms no indicator/disable; multi-sec writes leave button clickable → loading + disable trigger [recommend]
- [CONTROL] (Med) `app/web/admin/network_onts_actions.py:1074` — ping count hardcoded `Form(4)`, no UI field → per-invocation option (default 4, range 1-100) [recommend]
- [CONTROL] (Med) `app/web/admin/network_ont_service_ports.py:80` — tag_transform default literal "translate"; varies by vendor → per-OLT default setting [defer]
- [CONTROL] (Low) `network_onts_actions.py:1804` — HTTP-mgmt port silently coerces non-numeric to 80 → validate and reject [defer]

### TR-069 / ACS / CPE
- [POLISH] (High) `templates/admin/network/tr069/index.html:158-175` — bulk Reboot-All / Factory-Reset-All no confirmation → onsubmit confirm naming action + count [recommend]
- [POLISH] (High) `templates/admin/network/cpes/_tr069_partial.html:19` — single CPE Reboot no confirm → add hx-confirm [recommend]
- [POLISH] (High) `app/web/admin/network_cpes.py:371-382` + partial — factory-reset endpoint exists but no per-device button (only bulk) → surface button (w/ confirm) or remove orphan route [recommend]
- [POLISH] (High) `cpes/_tr069_partial.html:9-23` — Refresh/Connection-Request/Reboot use `hx-swap="none"`; after Refresh data not re-rendered → swap the /tr069 partial [recommend]
- [POLISH] (Med) `cpes/_tr069_partial.html:9-23,167-177` — no loading/disabled state; double-clicks queue dup ACS tasks; port toggle never reflects new state → indicator + disabled-while-pending + swap row [recommend]
- [POLISH] (Med) `cpes/_tr069_partial.html` — no visible async task result; "appears after next inform" but no task list/poll → show last-action status / link task list [defer]
- [POLISH] (Med) `cpes/_tr069_partial.html:249-253` — Traceroute one-click hardcoded 8.8.8.8 + inline JS blob while Ping has a modal → give traceroute same modal+input; extract shared helper [recommend]
- [POLISH] (Low) `templates/admin/network/tr069/tasks.html` — pending-task view no auto-refresh → add `hx-trigger="every 15s"` / refresh button [defer]
- [CONTROL] (Med) `tr069/index.html:558-568` — per-device firmware push is free-text `firmware_url`, no link to `OntFirmwareImage` catalog → catalog dropdown default, raw URL override [recommend]
- [CONTROL] (Med) `app/services/network/cpe_action_wifi.py:152-156` — LAN-port toggle hard-rejects ports outside 1–4; vendor-dependent → derive range from vendor capability (1–4 fallback) [defer]
- [CONTROL] (Low) `app/services/web_network_tr069.py:213` — `validate_acs_connection` hardcoded `timeout=5.0` → make a setting (default 5s) [defer]
- [CONTROL] (Low) `app/services/genieacs_client.py:296-330,581-582` — task-wait log interval/max-pending/UI timeout env-only → leave env; expose per-ACS only if needed [defer]

### Topology / monitoring / map / outages / performance
- [POLISH] (High) `monitoring/index.html:36` + `web_network_monitoring.py:80` — "Last updated … Africa/Lagos" label but value is `datetime.now(UTC)` (off +1h) → render in Lagos / drop label / show UTC [recommend]
- [POLISH] (High) `monitoring/index.html:187` + `web_network_monitoring.py:380` — "ONU Status Trend (24h)" returns one point → drive from time-series or retitle "Current ONU Status" [recommend]
- [POLISH] (Med) `monitoring/_kpi_partial.html:8-14` + `index.html:103-115` — "Live · HH:MM:SS" badge but refresh-on-demand only → add `hx-trigger="every Ns"` or relabel "Snapshot" [recommend]
- [POLISH] (Med) `topology/index.html:77-80` vs `186/251` — legend documents only edge colors; node circles colored by device status with no legend → add node-status legend [recommend]
- [POLISH] (Med) `map.html:683` — Leaflet map no loading/empty/tile-error; Refresh = full reload, no last-updated → add states + data-as-of [defer]
- [CONTROL] (High) `app/services/network_topology.py:423-430` — link-utilization thresholds (90/70/40) hardcoded → network settings (defaults 90/70, range 50-99) [recommend]
- [CONTROL] (High) `monitoring/index.html:309-323` — device-health color thresholds (CPU >85/>60, Mem >90/>70, Temp >70/>55) hardcoded in template → settings/alert-rule thresholds [recommend]
- [CONTROL] (Med) `app/services/topology/outage.py:27` — `STALE_OPEN_HOURS=36` module constant → setting (default 36h, range 6-168) [recommend]
- [CONTROL] (Med) `app/services/web_network_monitoring.py:218` — VPN tunnel "stale" `timedelta(minutes=3)` hardcoded → setting (default 3m) [defer]
- [CONTROL] (Med) `app/services/device_operational_status.py:42` + `topology/selfcare.py:37` — `_WARM_STALE_SECONDS=600` duplicated + hardcoded → single configurable value, dedupe [recommend]
- [CONTROL] (Med) `app/services/web_network_monitoring.py:24-28,642-648` — `_TUNNEL_NAMES` (pubkey→site) + `subnet_names` (/16→label) baked in source → DB/settings/lookup [recommend]
- [CONTROL] (Low) `app/web/admin/network_monitoring.py:141` (`days=365`) + `performance/index.html:16` — availability window fixed 365d, uptime badge cut-offs (99.5/98) separate from `infra_sla_target_percent` → window selector + derive badge from SLA target [defer]

### IPAM / fiber / pop-sites / zones / device-groups / core-devices
- [CONTROL] (High) `app/web/admin/network_core_devices.py:716` — backup SSH port defaults non-standard 120, no override → ssh_port setting (default 22) + per-device override [recommend]
- [CONTROL] (High) `app/services/web_network_fdh.py:319-320` — splitter port counts hardcoded 1-in/8-out; 1:16/1:32 need code edit → admin/vendor-model setting [recommend]
- [POLISH] (High) `templates/admin/network/ip-management/pool_form.html:80-85` — IPv4 mask preset only /24–/28; /22/23/29/30 unpickable → widen options / allow free prefix [recommend]
- [CONTROL] (Med) `network_core_devices.py:547-580` — per-graph defaults hardcoded (color/height/unit/type/factor) → graph_defaults settings/presets [recommend]
- [POLISH] (Med) `templates/admin/network/zones/detail.html` — no delete/archive + confirm for zones (device-groups have it) → add archive endpoint + confirm [recommend]
- [CONTROL] (Med) `app/services/web_network_fiber.py:107-127` & `web_network_fdh.py:34,229` — closure/cabinet/strand lists hard-capped 200, no pagination → limit/offset + pager [recommend]
- [POLISH] (Med) `templates/admin/network/ip-management/*.html` — long IP ops (import/bulk-assign/reconcile) no busy state → add submit-disable [defer]
- [POLISH] (Med) `templates/admin/network/pop-sites/detail.html:623-624` — map zoom hardcoded → map_zoom_* setting (country restriction already configurable via country_codes, `geocoding.py:70`) [defer]
- [CONTROL] (Med) `network_core_devices.py:114` — backup "stale" threshold hardcoded 24h → backup_stale_hours setting [defer]
- [POLISH] (Med) `templates/admin/network/fiber/change_request_detail.html:99-117` — approve/reject no submit-disable, no success toast → add disabled state + flash [defer]
- [CONTROL] (Med) `network_core_devices.py:79` — pagination choices [25,50,100,200]/default 50 hardcoded → settings (consistent across lists) [defer]
- [CONTROL] (Low) `app/web/admin/network_authorization_presets.py:156` — preset priority default 0, no seeded default, no documented tiebreaker → seed tool + document priority/inheritance [defer]
- Verified-clean: IPv4/IPv6 release confirms w/ audit copy, bulk-assign per-row success/error tables, content-aware CIDR validation, per-pool IPv6 delegation_prefix_length configurable, pool utilization chart empty-state.

### WireGuard / VPN / DNS-threats / speed
- [POLISH] (High) `templates/admin/network/vpn/index.html:208` — "Regenerate Keys" POST no confirm, breaks every peer connection → confirm dialog [recommend]
- [POLISH] (High) `vpn/index.html:258-264` (and `:62-66`) — Stop/undeploy tears down live interface/tunnels, no confirm/loading; same for Reload/Deploy/Restart → confirm on Stop/Restart + disable-on-submit [recommend]
- [POLISH] (Med) `app/web/admin/network_speedtests.py:85-96` — `speedtests_analytics` no table-missing guard, 500s if table absent (list route uses safe fallback) → same safe fallback [recommend]
- [POLISH] (Med) `templates/admin/network/speedtests/index.html:99-102` — filter exposes only "From" though backend supports `date_to` → add "To" input [recommend]
- [POLISH] (Low) `templates/admin/network/dns_threats/index.html:81-89` — filter omits `network_device_id` though backend accepts it → add device filter [defer]
- [POLISH] (Low) `vpn/peer_detail.html:260,289` — Copy no visual confirm; regen-script error uses raw `alert()` → emit toast [defer]
- [CONTROL] (High) `app/services/web_network_speedtests.py:692` — SLA "underperforming" ratio `< 0.8` hardcoded → setting `speedtest_sla_ratio` (default 0.8, 0.0-1.0) [recommend]
- [CONTROL] (Med) `app/services/wireguard.py:526` (+ `:697`) — provisioning token TTL hardcoded 24h (regenerate already parameterizes) → setting + per-action TTL (default 24, 1-168) [recommend]
- [CONTROL] (Med) `templates/admin/network/vpn/peer_form.html:95` — keepalive default 25 hardcoded while port/MTU/address from `get_vpn_defaults` → add `wireguard_default_keepalive` to same map [defer]
- [CONTROL] (Med) `app/services/wireguard.py:1358,1437` — RouterOS API pool `ssl_verify=False` even when SSL on → per-server `router_api_ssl_verify` (default off, recommend on) [defer]
- [CONTROL] (Low) `app/services/wireguard.py:1274` — log retention default 90d hardcoded → `wireguard_log_retention_days` setting [defer]
- [CONTROL] (Low) `app/services/wireguard.py:423` — handshake "connected" window `< 180s` hardcoded → optional `wireguard_handshake_online_secs` [defer]
- Already-configurable: listen-port/MTU/VPN-address/interface via `get_vpn_defaults`; map customer cap (`network_map.py:307`).
