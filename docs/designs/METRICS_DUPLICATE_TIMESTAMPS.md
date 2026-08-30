# Duplicate-timestamp collisions in Sub's federated metrics

**Status:** diagnosis, repository-side. The repair is not applied — it changes a
label every downstream query depends on, so it needs the Observability lane's
agreement first.

## The symptom

Sub's VictoriaMetrics runs with `-dedup.minScrapeInterval=1ms` and has recorded
**67,827 select-time dedups**. **171 `(series, timestamp)` pairs return two
values**, all under `job="dotmac-app"` / `instance="dotmac-app"`, in exactly
four families:

```
http_requests_created
http_request_duration_seconds_created
http_request_duration_seconds_sum
redis_operations_created
```

The central observe Prometheus drops these on federation — "different value but
same timestamp". Observability's relabeling was cleared as the cause: zero
collisions across 312 real payloads.

## Where the four series come from

All four are process-local samples exported by the single `/metrics` endpoint
(`app/main.py:1680`, `generate_latest()` over the default registry):

| Sample | Defined at | Family |
|---|---|---|
| `http_requests_created` | `app/metrics.py:11` — `Counter("http_requests_total", …)`; the client strips `_total` for the `_created` sibling | per-collector |
| `http_request_duration_seconds_created` / `_sum` | `app/metrics.py:16` — `Histogram(…)` | per-collector / per-process accumulator |
| `redis_operations_created` | `app/services/redis_metrics.py:45` — `Counter("redis_operations_total", …)` | per-collector |

**That set is the diagnosis.** `_created` is written once, when a collector is
constructed in a process; `_sum` accumulates within a process. They are exactly
the samples that differ between two processes exporting the same metric names.
A relabeling or ingestion fault would not select for them.

## Ruled out: re-import

`docker-compose.yml` justifies the dedup flag by naming four app import writers
— `metrics_store`, `bandwidth_metrics_adapter`, `monitoring_metrics`,
`olt_polling_metrics` — and describes "a re-imported sample at the same
millisecond timestamp".

**That explanation does not cover these four series.** `metrics_store` is a
bandwidth-samples client for VictoriaMetrics; the other three emit GenieACS
fleet, bandwidth-aggregate and OLT-polling series. None of them touches HTTP or
Redis client metrics, and nothing in `app/` re-imports the default registry.

The comment is not wrong about why the flag was added; it is incomplete as an
explanation of what is colliding now, and it sends the next reader to the wrong
place. Worth correcting when the repair lands.

## The two contributing conditions

**1. The scrape config erases the only distinguishing identity.**
`config/vmagent/config.yml` has a single job whose `relabel_configs` overwrite
`instance` with the constant `dotmac-app`:

```yaml
      - source_labels: [__address__]
        target_label: instance
        replacement: 'dotmac-app'
```

Whatever separates two exporting sources — address, port, container — is
discarded before ingestion, so two sources become one series identity. This is
the "real identity that distinguishes the two series" the repair must preserve.

**2. The exporter assumes one process, and the runtime makes that a knob.**
`app/metrics.py` states the assumption twice — *"no multiprocess mode, workers
recycle"* (lines 153, 1081) — and `app/services/poller_health.py` repeats it.
But `docker-compose.yml:73` runs
`uvicorn app.main:app --workers ${WEB_CONCURRENCY:-1}`, and
`docs/UI_EDGE_CASES.md` already records `WEB_CONCURRENCY>1` as a real
deployment shape ("in-memory per-worker … weaker under `WEB_CONCURRENCY>1`").

With more than one worker and no `prometheus_client` multiprocess mode, each
worker holds an independent registry with its own `_created` timestamps and its
own `_sum` totals. That is a latent defect on its own: it makes every
process-local metric depend on which worker answered the scrape.

**What production observation would settle it:** the deployed `WEB_CONCURRENCY`
value, and whether the `app` service runs more than one container. Those are
host-side and are not read here.

## Repair, within the stated constraints

Michael ruled out `honor_timestamps: false`, broad sample drops and arbitrary
deduplication — each hides the loss instead of removing it, and the dedup flag
already in place is why this went uncounted for so long. Two admissible
directions, and they compose:

1. **Retire the duplicate writer.** Give the scraped process a single exporting
   identity: either one web worker, or adopt `prometheus_client` multiprocess
   mode (`PROMETHEUS_MULTIPROC_DIR`) so N workers export one coherent registry.
   Multiprocess mode changes `_created`/`_sum` semantics and drops some
   collector types, so it is a real design change rather than a flag.
2. **Stop erasing identity.** Let `instance` keep something that distinguishes
   sources instead of a constant.

**Direction 2 is not a free one-line change.** Every dashboard, alert and
recorded query that matches `instance="dotmac-app"` depends on that constant,
and changing it creates a series discontinuity across the cutover. It needs the
Observability lane's agreement and a plan for the discontinuity — which is why
this document stops at the diagnosis.

Once a single identity is restored, `-dedup.minScrapeInterval=1ms` should be
re-evaluated rather than kept indefinitely: it is currently masking the
remainder, and a dedup that no longer has anything to dedup is a setting whose
next reader will mistake it for load-bearing.
