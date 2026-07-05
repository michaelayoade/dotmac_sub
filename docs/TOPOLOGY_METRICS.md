# Topology coverage + pipeline-health metrics

"Is the graph healthy?" is a paged-on number, not a report. The
`topology_metrics_export` beat task (default every 900s, floor 300s, setting
`network_monitoring.topology_metrics_interval_seconds`) derives per-medium E2E
coverage from the same batched classification the topology-gaps page renders
(`app/services/topology/gaps.classify_active_subscriptions`) and pushes gauges
to the app's VictoriaMetrics container through the existing bandwidth push
path (`VictoriaMetricsWriter`, `VICTORIAMETRICS_URL`,
`/api/v1/import/prometheus`).

Motivating incident: a `uisp_sync` run returned `failed=629` while the Celery
task itself "succeeded". Nothing read the stats dict, so nobody was paged and
the graph quietly rotted until someone opened the gaps page.

## Series

| Series | Labels | Meaning |
| --- | --- | --- |
| `topology_e2e_coverage_ratio` | `medium=fiber\|wireless\|nas\|unknown` | resolved complete paths / active subscriptions on that medium (emitted only when the medium has active subs) |
| `topology_subscribers_active` | `medium` | active subscriptions per medium (zeros emitted) |
| `topology_subscribers_gapped` | `medium`, `gap=no_ont\|no_node\|no_basestation` | gapped subscriptions per medium+gap kind — full cross-product emitted with zeros so cleared gaps drop to 0 |
| `topology_task_last_result` | `task=uisp_sync\|lldp_poll`, `counter=<stats key>` | last run's returned stats dict, one series per counter (`created`, `failed`, `unmatched_no_subscriber`, ...); non-numeric values (e.g. `error`) emit presence=1 |
| `topology_task_staleness_seconds` | `task` | seconds since the feeder task last stashed a run result; `1e9` sentinel when never run (or stash aged out after 7d) |
| `topology_source_freshness_seconds` | `source=uisp\|lldp` | seconds since `max(cpe_devices.uisp_synced_at)` / `max(network_topology_links.last_seen_at where source='lldp_neighbor')`; `1e9` sentinel when no rows |

`medium=unknown` is the no-access-device case (always `gap=no_ont`): no ONT,
no resolvable radio, no provisioning NAS.

## Suggested Grafana alert rules

Ops wiring on the observe host is out of scope here; these are the intended
starting rules.

Coverage regression (drop vs 24h ago, per medium):

```promql
topology_e2e_coverage_ratio
  < (max_over_time(topology_e2e_coverage_ratio[24h] offset 5m) - 0.02)
```

Feeder failed counters (the `failed=629` case). PromQL label regexes are
fully anchored, so the pattern must cover the real counter names:
`failed` and `port_fetch_failures` (uisp_sync), `nas_failed` (lldp_poll),
and the `error` presence marker emitted by failed runs:

```promql
max by (task) (topology_task_last_result{counter=~".*fail.*|.*error.*"}) > 0
```

Feeder staleness (also fires on never-run, thanks to the sentinel):

```promql
topology_task_staleness_seconds{task="uisp_sync"} > 3600
topology_task_staleness_seconds{task="lldp_poll"} > 14400
```

Source data freshness:

```promql
topology_source_freshness_seconds{source="uisp"} > 3600
topology_source_freshness_seconds{source="lldp"} > 14400
```

Exporter absence (alert if the exporter itself dies):

```promql
absent_over_time(topology_subscribers_active[1h])
```
