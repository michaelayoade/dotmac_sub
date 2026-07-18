# Channel & Webhook Observability

**Owner:** `observability` (facts) + Alertmanager on `dotmac-observe` (consequences)
**Status:** Proposed — Sub-side instrumentation and alert rules ready; export to dotmac-observe gated on host access
**Motivates:** the 2026-07-18 crm.dotmac.io inbox flood

## The incident this exists to catch

A full-stack restart flushed two backlogs through two ingestion paths at once,
and nothing alerted. The worker had been silently down for hours; the only
symptom was the delayed flood. **The dangerous failure was the silence, not the
flood** — so the first-class signal here is *a sensitive channel going quiet*,
and every failure mode below is a real one this class of incident exhibits.

## How Sub's inbound actually works (and how it differs from the incident)

The incident was on the CRM stack, whose email path polled IMAP with a
last-seen-UID cursor and whose webhooks *enqueued* for a worker. Sub is
different, and the design must match Sub, not CRM:

- **Inbound is entirely push.** WhatsApp, Messenger/Instagram, and CRM arrive
  as HMAC-verified webhooks (`app/api/inbox_webhooks.py`,
  `meta_inbox_webhooks.py`, `crm_webhooks.py`); email arrives over SMTP
  (`app/services/team_inbox_smtp_inbound.py`). There is no poller and no cursor,
  so the incident's cursor-reset re-ingestion cannot occur here.
- **Webhooks are processed inline**, not enqueued — the route calls
  `team_inbox_channel_receive.receive_inbound_channel` and commits in-request.
  So Sub has no "worker stalls, queue drains in a burst" mode for inbound
  either. Its equivalent failure is inline latency: if processing slows, the
  provider times out, retries, and eventually drops — silent inbound loss.
- **Dedup is a DB partial-unique index** on `(channel_type,
  external_message_id) where direction='inbound'`. Redelivery is suppressed at
  write time, so a provider retry storm shows up as a spike in *suppressed*
  inbound, not as duplicate rows.

The push model trades the incident's two failure modes for three of its own,
which is what this design instruments.

## Signals

Four signal groups. Two are web-process Counters (webhook and dedup events
happen in the web process that serves `/metrics`); two are worker-produced
snapshots exported through the existing `_ObservabilityStateCollector`
(`app/services/observability.py` `publish_state_snapshot` → `app/metrics.py`),
which exists because a gauge set in a Celery worker is invisible to the web
process.

### 1. Channel ingestion freshness — the dead-man's-switch

Worker snapshot, domain `channel_ingestion`. Per channel scope (`whatsapp`,
`email`, `facebook_messenger`, `instagram_dm`, `chat_widget`, `sms`):

| signal | meaning | alert |
| --- | --- | --- |
| `seconds_since_last_inbound` | age of the newest inbound message for the channel | **silence**: exceeds the channel's expected quiet window during business hours |
| `inbound_count_15m` | inbound rows written in the last 15 min | context for the silence alert; also a flood ceiling |

This is the signal the incident was missing. It fires when a channel stops
producing — the earliest warning that an endpoint or the SMTP intake is down,
before anyone notices missing conversations. It is channel-shaped, so a per-
channel expected-quiet window lets a low-volume channel (email) and a high-
volume one (WhatsApp) share one rule.

### 2. Webhook receipt, latency, and errors

Web-process instruments, incremented in each webhook route:

- `sub_webhook_events_total{provider, event, outcome}` — `outcome ∈ {accepted,
  rejected, error}`. `rejected` is a failed HMAC/verification; `error` is an
  unhandled processing failure.
- `sub_webhook_processing_seconds{provider, event}` histogram — because Sub
  processes inline, this latency *is* the deliver-or-drop margin. Rising
  latency predicts provider-side drops before they happen.

A rising `error`/`rejected` rate is the only in-app signal that we are losing
deliveries the provider still believes it made.

### 3. Inbound redelivery pressure

Web-process Counter `sub_inbound_dedup_suppressed_total{channel}`, incremented
where the receive path finds an existing `external_message_id`. A healthy
baseline is near zero. A spike is the Sub-native signature of a provider retry
storm — the push-model analogue of the incident's flood — visible without any
duplicate ever reaching an agent's inbox.

### 4. Async queue depth and worker liveness

Worker snapshot, domain `celery_queues`. Sub routes async work across several
Redis queues (`celery`, `billing`, `ingestion`, `crm`, `tr069`, `acs`,
`bandwidth`, `nin`). Per queue scope:

| signal | meaning | alert |
| --- | --- | --- |
| `queue_depth` | `LLEN` of the Redis queue list | sustained growth = a worker not draining |

Inbound is inline so this does not guard the inbox directly, but it guards
every other operational path (billing runs, provisioning, outbound
notifications) against the exact worker-stall the incident hinged on. It reads
Redis `LLEN` only — bounded, no broker introspection. (Head-of-queue task age
would need decoding the broker's message envelope; depth alone is the reliable
signal and is what the producer emits.)

## Producer

One scheduled beat task, `observe_channel_health`, runs about every 60s and
publishes the two worker snapshots in a single pass: one `MAX(received_at)` and
one 15-minute count per channel for freshness, one `LLEN` per queue for depth.
It writes facts only — it never mutates inbox, queue, or delivery state, so it
stays a pure observer and does not call any transport, keeping it clear of the
consent/transport-ownership boundary
(`tests/architecture/test_communication_eligibility_ownership.py`).

## A gap this surfaced

`start_smtp_inbound_server` has no caller in the repo — no launcher in
`scripts/`, deploy, Dockerfiles, or compose. If email is expected to be a live
inbound channel in Sub, the SMTP intake may not be running at all. Signal 1
makes this visible immediately (email `seconds_since_last_inbound` climbs
without bound); it is worth confirming out-of-band whether that intake is
wired before relying on the channel.

## Export to dotmac-observe

Sub already exposes `/metrics` (`app/main.py`) and runs
`dotmac_sub_victoriametrics` + promtail on prod; `dotmac-observe` runs
Prometheus/Alertmanager/Grafana/Loki. Wiring:

1. Add Sub's `/metrics` as a scrape target on `dotmac-observe` Prometheus, or
   federate/remote-write from the Sub-side VictoriaMetrics — match whichever
   the existing Sub scrape uses; confirm on the host.
2. Load `deploy/observability/channel_observability.rules.yml` into Alertmanager's rules.
3. Route these alerts to the on-call receiver.

**Blocked:** the observe host rejected key auth during the incident. Deploying
the scrape config and rules needs Michael to name and authorize that host. The
Sub-side instrumentation ships independently of that step.

## Alert rules

See `deploy/observability/channel_observability.rules.yml`. Thresholds are starting points
and must be tuned against real series before the silence alert is trusted for
paging rather than ticketing — a per-channel expected-quiet window especially,
since a legitimately idle overnight email channel must not page.
