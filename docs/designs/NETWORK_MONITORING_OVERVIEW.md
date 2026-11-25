# Network monitoring overview page contract

Status: implemented. Audience: NOC operators. Screen: `/admin/network/monitoring`, dashboard.

## Decision and ownership

The first viewport answers which operational exceptions need investigation now.
`network.device_state` supplies device operation, `network.monitoring_inventory`
supplies alert records, and `network.ont_runtime_status` supplies ONT counts.
The web read service composes these existing owners; it does not reinterpret
their states or own a new transaction. The page keeps the existing semantic
color tokens and `monitoring:read` permission.

## Information and actions

- Glance: device failures, open and critical alarms, ONTs not working, low signal,
  and the snapshot time. Four KPI links lead to the relevant work views.
- Work: a bounded open-alarm preview loads after the initial response. The
  fleetwide PON outage scan runs only on request; the full NOC queue and alarms
  page remain the authoritative investigation paths.
- Investigation: the paginated device inventory and its existing bulk actions
  move to `/admin/network/monitoring/devices`. Existing search and pagination
  query links redirect there. Bulk controls require `monitoring:write`, and device-detail links require `network:device:read`. Bandwidth and site/VPN detail load on request.
- Evidence and trends: use existing alarms, performance, worklist, and device
  detail views. No charts or audit feed block the overview response.

The initial response performs no VictoriaMetrics query or WireGuard command.
Secondary requests have independent loading and error text. The bandwidth
response uses the existing 10-second process-local cache; it is not a new
source of truth. Device pagination remains server-side and capped at 100 rows.
The overview reports current owner-resolved counts; its timestamp records when
the read completed, not when every underlying observation was made.

Validation: the focused route and read-model tests assert the split and correct
alarm counts. After deployment, measure overview response time, deferred panel
time, database query counts, and browser layout shift against the prior page.
