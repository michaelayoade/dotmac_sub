# ADR 0018: CoA outcome evidence is a durable, out-of-band counter

Status: proposed

Date: 2026-10-12

Decision owner: Michael (approved 2026-10-12)

Affected systems and domains: network access control plane
(`access.session_enforcement`), router health reconciler
(`network.router_health`), observability.

Numbering note: other documents in this repository write "ADR-0018" for a
Governance-repository decision. This file is Sub's own ADR 0018.

## Context

RADIUS CoA (Disconnect-Request and CoA-Update) is an irreversible device effect.
`_send_coa_disconnect` returns a typed `CoADisconnectOutcome`, and
`_send_coa_update` returns a bare `bool`, but neither outcome is recorded
anywhere durable. The only memory is a 15-minute in-process negative cache and
log lines. On 2026-10-12 the Gwarimpa Access router (RouterOS 7.15.1) was
found to receive CoA and never reply: the evidence existed only in logs, and
nothing in Sub could say "this NAS has not answered CoA for days". API and SSH
enforcement outcomes already have a durable record (ADR 0017); CoA does not.

## Decision

1. **Authoritative record.** A new observation table, `nas_coa_outcome_buckets`,
   holds one counter row per (NAS device, kind, disposition, UTC hour). Kind is
   `disconnect`, `update` or `canary`; disposition is one of the twelve
   `CoADisconnectDisposition` values or `sent` (a CoA-Update that was sent). It
   is evidence of what happened, never intended access state.

2. **Canonical writer.** `network.coa_outcome_evidence`
   (`app/services/router_health/coa_evidence.py`, `record_coa_outcome`) is the
   only writer. `access.session_enforcement` calls it once at every return of
   `_send_coa_disconnect` and `_send_coa_update` (except `disabled`), and the
   router-health canary records its own `canary` kind. A bounded retention
   prune (`prune_coa_outcome_buckets`, 35 days) lives in the same module.

3. **Approved exception: out-of-band evidence write.** The service uses
   transaction mode `out_of_band_evidence`. The writer opens its own unit of
   work with `db_session_adapter.create_session()`, runs
   `INSERT ... ON CONFLICT DO UPDATE SET count = count + 1`, commits and
   closes. It never joins the caller's transaction for two reasons:
   - the evidence of an irreversible device effect must survive the caller's
     rollback (enforcement handlers roll back precisely when a step fails);
   - a hot per-NAS-hour row lock taken inside the caller's transaction would
     serialise enforcement across subscriptions.

   The exception is limited to this one writer, id-only keys with **no foreign
   key** (the caller may hold row locks that an FK check from a second
   connection would wait on), and a record the calling transaction never reads
   back.

4. **Bounds.** The write sets `SET LOCAL statement_timeout = '500ms'` and
   `lock_timeout = '200ms'`. It never raises into the caller; the only
   exception re-raised is a Celery soft time limit. A failed write is logged at
   `ERROR` and counted in
   `OBSERVABILITY_EVENTS_TOTAL{domain="coa_evidence", status="write_failed"}`.
   A process-local circuit breaker skips writes for 60 seconds after three
   consecutive failures, so a database problem costs the enforcement path at
   most a few bounded waits, not one per CoA. Every recorded outcome also
   increments `OBSERVABILITY_EVENTS_TOTAL{domain="coa", signal=<kind>,
   status=<disposition>}`.

5. **Readers.** `network.router_health` reads the counters
   (`reconcile_service_path`) to raise `coa-timeouts` findings. No reader may
   treat a bucket as the intended state of a subscription.

6. **Canary safety (added in review).**
   - *Collision check, exactly what is covered:* before each probe the
     synthetic `User-Name` is checked against local `access_credentials` and
     `radius_users`, and against the external FreeRADIUS `radacct.username`
     of every configured external RADIUS DB (read-only, one `SELECT ... LIMIT 1`
     with a 500 ms server-side `statement_timeout`). If a lookup errors, the
     probe is NOT sent and the run records `collision_check_unavailable` (a
     medium `coa-canary-config` finding; no outcome bucket). With no external
     DB configured there is nothing external to collide with and only the local
     tables are checked. The name carries a non-producible prefix plus 64 random
     bits, so a collision is not expected in practice.
   - *Not covered by the 500 ms timeout:* the time to obtain a database
     connection (pool checkout, TCP connect to the external DB) and, for the
     evidence write, the wait for a connection before `SET LOCAL
     statement_timeout` takes effect. This is the same gap as ADR 0017.
   - *Time budget:* a canary run stops scheduling new probes when the remaining
     budget (12 minutes of the 15 minute soft limit) is below one spacing
     interval; unprobed targets get no bucket and are picked up by the next
     run (`deferred` counter).
   - *Re-probe locking and escalation:* the re-probe task locks per NAS, so
     re-probes for different NAS never skip each other. A first-probe failure
     whose re-probe has not reported within `max(3 x reprobe delay, 15 min)` is
     escalated by `reconcile_service_path` to a high `coa-canary-unverified`
     finding instead of staying pending; the next healthy probe resolves it.
   - *Duplicate NAS rows:* rows sharing host and CoA port are probed once, using
     the row with a local `shared_secret` (ties by id). This matches
     enforcement's secret resolution order (local secret first, then the
     external `nas` table).

## Invariants

- A CoA outcome counter is written only by `network.coa_outcome_evidence`
  (`tests/architecture/test_router_health_single_writer.py`).
- Recording an outcome can never change the enforcement result that follows it
  (`tests/services/router_health/test_enforcement_coa_instrumentation.py`).
- The negative cache is untouched by the canary.

## Consequences

- One small extra database write per CoA, bounded at 500 ms and normally a few
  milliseconds; during bulk suspensions this adds roughly a few ms per CoA.
- Rows are bounded: kinds x dispositions x NAS x hours, pruned at 35 days.
- Counters lose precision on write failure by design; they are a health signal,
  not an audit ledger.

## Migration and cutover

- Old owner and paths: none (outcomes were logs only).
- New owner and paths: `network.coa_outcome_evidence`; migration 672.
- Backfill/repair: none; counters start filling on deploy.
- Shadow or verification phase: counters fill with all controls off; findings
  stay in shadow mode (`router_health_alert_mode=shadow`).
- Cutover gate and evidence: `SELECT kind, disposition, sum(count)` after 24 h.
- Fallback retirement: not applicable.
- Schema contract step: not applicable (new table).

## Verification

`tests/services/router_health/test_coa_evidence.py`,
`tests/services/router_health/test_enforcement_coa_instrumentation.py`,
`tests/architecture/test_out_of_band_evidence_ratchet.py`,
`tests/architecture/test_undeclared_out_of_band_writers.py`,
`tests/architecture/test_router_health_single_writer.py`.

## Rollback or forward-fix

Revert the deploy; migration 672 may stay (the tables are inert). `downgrade()`
drops the tables and discards the evidence, which is acceptable before the
reconciler is relied on.

## Review and retirement

- Review date: after the 7-day shadow soak of the router health reconciler.
- A shared out-of-band evidence helper remains the extraction candidate named
  in ADR 0017; this is the second user of the pattern after enforcement
  evidence.
