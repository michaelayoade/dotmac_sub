# CRM ticket capability cutover

This runbook completes the explicit `dotmac.crm` ticket-observation cutover
after migration 377 disabled legacy jobs that had no capability binding.

The operation is not a database patch:

- `integration.installations` adds `crm.ticket_observation.v1`, validates the
  pinned connector and existing configuration, performs a live non-mutating CRM
  connection check, and atomically restores the enabled installation/bindings.
- `integration.jobs` binds and activates the reviewed `Pull CRM Tickets` job.
- `scheduler.registry` remains the only cadence owner. The job stays `manual`;
  the `crm.ticket_pull` control schedules incremental and daily reconciliation.

The two owner commands are independently idempotent. Scheduler and webhook
adapters fail closed between them, so a partial operation cannot start an
unbound background job or silently accept an unexecutable ticket event.

## Preconditions

1. Name and verify the target host.
2. Use an immutable candidate image whose OCI revision matches the reviewed
   release.
3. Confirm the deployed `dotmac.crm` manifest pin is available.
4. Do not retrieve or print CRM secret values. The installation owner resolves
   its existing approved references only inside connection validation.

## Preview

Run the candidate image against the target database without changing state:

```bash
APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-<reviewed-sha> \
docker compose -f docker-compose.yml run --rm --no-deps app \
  python -m scripts.integrations.reconcile_crm_ticket_capability
```

Review all of the following:

- exactly one production `dotmac.crm` installation;
- its exact installation ID, connector version, and manifest digest;
- exactly one CRM target job named `Pull CRM Tickets`;
- the current binding/job state;
- `eligible: true`;
- the exact preview `fingerprint`.

An enabled `crm.ticket_pull` control with zero enabled ticket bindings and zero
active ticket jobs is the expected pre-cutover production state. Any ambiguity,
unexpected existing binding, different job binding, unavailable manifest, or
unvalidated installation must be resolved before apply.

## Apply

Apply only the reviewed IDs and fingerprint:

```bash
APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-<reviewed-sha> \
docker compose -f docker-compose.yml run --rm --no-deps app \
  python -m scripts.integrations.reconcile_crm_ticket_capability \
  --apply \
  --installation-id <reviewed-installation-uuid> \
  --job-id <reviewed-job-uuid> \
  --expected-fingerprint <reviewed-sha256> \
  --idempotency-key <reviewed-operation-key> \
  --actor <operator-identity> \
  --reason "<reviewed reason>"
```

The command exits non-zero if the preview changed, connection validation
failed, either owner rejected its exact state, or final readiness is not green.
If the installation step succeeds but job activation fails, do not edit tables.
Run preview again and safely replay the operation with the new reviewed
fingerprint.

## Verify and deploy

The final apply output must report:

- `already_ready: true`;
- one enabled ticket-observation binding;
- one active ticket job bound to that binding;
- `schedule_enabled: true`.

Run the independent deployment gate:

```bash
APP_IMAGE=ghcr.io/michaelayoade/dotmac_sub:sha-<reviewed-sha> \
docker compose -f docker-compose.yml run --rm --no-deps app \
  python -m scripts.integrations.verify_crm_ticket_readiness
```

Then run the normal guarded deployment. `scripts/deploy.sh` repeats this check
before starting the warm candidate.

After deployment, verify:

1. the five-minute pull produces a successful `IntegrationRun`;
2. one CRM ticket read succeeds without `InstallationError`;
3. a reviewed webhook test queues `sync_crm_ticket`;
4. no new `no active CRM ticket capability job configured` or
   `no enabled binding for crm.ticket_observation.v1` errors appear.

## Recovery

If the provider connection becomes unavailable, disable `crm.ticket_pull`
through the canonical control owner before disabling the binding. Use the
installation and job admin owners for lifecycle changes; never direct-edit
`integration_installations`, `integration_capability_bindings`, or
`integration_jobs`.

---

# Retirement

The CRM was decommissioned on 2026-08-29. This poller is the surviving inbound
writer: every five minutes it creates `support_tickets` rows and stamps
`subscribers.crm_subscriber_id`, against a host that no longer exists.

It is recorded as **STILL LIVE** in the `old_writer_retirement` field of the
cutover receipt in `docs/runbooks/TEMPORARY_CRM_CHAT_AUTHORITY.md`. Under
Governance ADR 0018 rule 2 any STILL LIVE item blocks a decommission
declaration, so **retiring this is what makes the CRM decommissionable**, and
the last step of this procedure is to move that field to RETIRED.

## The sequence, and why the gate comes before the deletion

1. Turn the `crm.ticket_pull` control **off through the canonical settings
   owner** — see "Where the switch actually is" below. Setting the environment
   variable does nothing.
2. Observe at least **two** five-minute intervals.
3. Prove no CRM calls and no new CRM-derived writes — **with a positive
   control**, see below.
4. Delete the poller, its task registration, its settings, and the new
   `crm_subscriber_id` write paths.
5. Retain existing CRM identifiers as historical references only.
6. Add two-directional ratchets over the scheduler, task and config
   vocabulary — not only file paths.

Steps 1-3 are production operations and are the gate. Do not begin step 4
until step 3 has produced its evidence: once the code is gone, the flag is
moot and the observation can never be made.

## Where the switch actually is

**`CRM_TICKET_PULL_ENABLED` is inert. Setting it to `false` is a no-op.** An
earlier revision of this runbook said to set it, and that instruction would
have produced a poller that kept running while everyone believed it had been
stopped — the worst possible input to step 3.

The evidence, in the order it settles the question:

- `control_registry._resolve_own_flag_with_source` reads exactly one thing: the
  `domain_settings` row `domain='modules'`, `key='crm_ticket_pull'`, falling
  back to the registry `on_missing` default. Its docstring says it plainly —
  *"Retired environment and database aliases are deliberately ignored."*
- The `LegacyAlias(_SCH, "crm_ticket_pull_enabled", "CRM_TICKET_PULL_ENABLED")`
  on the control is **declaration-only**. Nothing in `app/` ever reads a
  `Control.legacy` attribute.
- Migration `309_retire_feature_aliases` materialised the old
  `scheduler.crm_ticket_pull_enabled` row into the canonical
  `modules.crm_ticket_pull` row, preserving its truthiness, and **deleted the
  legacy row** (its `retain_legacy` flag is `False`).
- The only consumer is `app/services/scheduler_config.py:2109`, calling
  `control_registry.is_enabled(session, "crm.ticket_pull")`.

So **the explicit source enabling the poller is a `domain_settings` row**, not
an environment seed:

```sql
-- The one row that decides. Expect value_text = 'true'.
SELECT id, domain, key, value_type, value_text, is_active, created_at, updated_at
  FROM domain_settings
 WHERE domain = CAST('modules' AS settingdomain)
   AND key = 'crm_ticket_pull';
-- Zero rows, or is_active false => the control is already off by registry
-- default (on_missing = False) and something else is running the poller.
```

**Change it through the canonical writer, never by editing the registry
default.** The default is already `False` and is correct; changing it would
both fix the wrong thing and mask whatever is overriding it. The canonical
writer is `control_registry.update_canonical_feature_controls`, whose single
production adapter is the admin system-settings POST at
`app/web/admin/system.py:433` (guarded by `system:settings:*`). It runs
`validate_feature_control_changes` first and returns a before/after record of
stored value, effective value and source — keep that record, it is part of the
retirement receipt.

### There is a second, independent gate

Even with the control on, the schedule is only registered if
`resolve_crm_ticket_pull_readiness` passes, which requires **all** of: an
`enabled` `dotmac.crm` installation, an `enabled` `crm.ticket_observation.v1`
binding, and an active bound job. So the poller can already be stopped by the
installation being disabled, and `scheduler_config.py:2137` would be logging
`crm_ticket_pull_not_ready` every beat instead.

Check both before concluding anything from step 3, and note that disabling the
installation or binding is a second kill switch — and the one that also retires
the transport for containment item 4.

### A dead control the orphan guard cannot see

`scheduler.crm_ticket_pull_enabled` is still a registered `SettingSpec` with
`env_var="CRM_TICKET_PULL_ENABLED"` (`app/services/settings_spec.py:1594`), so
the generic settings UI still offers it and nothing reads it.
`tests/architecture/test_no_orphan_settings.py` does not catch it, because the
guard counts any quoted occurrence of the key under `app/` as a reader — and
the `LegacyAlias` declaration that records the key as *retired* is itself such
an occurrence. Removing the spec is CRM-surface deletion and belongs to the
owning slice, not to containment; it is noted here so nobody reads that toggle
as live.

## Step 3: the observation, and the control that makes it mean anything

Two quiet intervals prove nothing on their own. They are equally consistent
with "the poller stopped" and "nobody was looking at the right table". The
observation is only an observation if the same query demonstrably produced
rows *before* the flag flip. That is the positive control, and this pull has
one already — it does not need to be built.

**Why the control exists.** `run_scheduled_pull`
(`app/services/integration_sync.py`) inserts an `IntegrationRun` row with
`trigger='scheduled'` and `requested_by='celery-beat'` **before** it calls the
CRM, and on failure sets `status='failed'`, records the error and **commits**.
So a poll against the dead CRM still writes a durable, attributed row. The
pre-flip window is therefore expected to be full of failed runs roughly five
minutes apart — which is exactly what proves the instrument works.

Run all four against `dotmac_sub`. Queries 1 and 2 are the control; 3 and 4
are the observation.

```sql
-- 1. CONTROL. The poll cadence before the flag flip. This MUST return rows
--    about five minutes apart, almost certainly status='failed' with an error
--    naming the dead host. If it returns nothing, STOP: either the poller was
--    already not running, or you have the wrong job, and in both cases the
--    silence in query 3 would prove nothing.
SELECT r.started_at, r.finished_at, r.status, r.trigger, r.requested_by,
       left(coalesce(r.error, ''), 120) AS error_head
  FROM integration_runs r
  JOIN integration_jobs j ON j.id = r.job_id
  JOIN integration_capability_bindings b
    ON b.id = j.capability_binding_id
 WHERE b.capability_id = 'crm.ticket_observation.v1'
   AND r.started_at >= now() - interval '2 hours'
 ORDER BY r.started_at DESC;

-- 2. CONTROL. The same instrument at the record level: what the pull wrote
--    per ticket when it last succeeded. Establishes that a successful pull is
--    visible here, so an empty query 4 is meaningful.
SELECT date_trunc('hour', rec.created_at) AS hour,
       rec.entity_type, rec.direction, rec.action, rec.status, count(*)
  FROM integration_records rec
  JOIN integration_runs r ON r.id = rec.run_id
  JOIN integration_jobs j ON j.id = r.job_id
  JOIN integration_capability_bindings b
    ON b.id = j.capability_binding_id
 WHERE b.capability_id = 'crm.ticket_observation.v1'
 GROUP BY 1, 2, 3, 4, 5
 ORDER BY 1 DESC
 LIMIT 20;

-- 3. OBSERVATION. No CRM calls since the flip. Run this at least TWO poll
--    intervals (>= 10 minutes) after setting the flag false. Zero rows is the
--    pass, and query 1 is what earns it the right to mean "stopped".
SELECT count(*) AS runs_since_flip
  FROM integration_runs r
  JOIN integration_jobs j ON j.id = r.job_id
  JOIN integration_capability_bindings b
    ON b.id = j.capability_binding_id
 WHERE b.capability_id = 'crm.ticket_observation.v1'
   AND r.started_at >= :flip_time;

-- 4. OBSERVATION. No new CRM-derived writes since the flip, checked at the
--    destination rather than at the writer. The provenance discriminator for
--    a CRM-derived ticket is the metadata key, not external_system.
SELECT count(*) AS crm_tickets_since_flip
  FROM support_tickets
 WHERE metadata ->> 'crm_ticket_id' IS NOT NULL
   AND created_at >= :flip_time;

SELECT count(*) AS subscribers_stamped_since_flip
  FROM subscribers
 WHERE crm_subscriber_id IS NOT NULL
   AND updated_at >= :flip_time;
```

Record the four results, the flip time and the observation time. That set is
the `runtime_observation` for this retirement's receipt — the field the chat
cutover could not fill, and the reason this procedure exists in this shape.

**Why query 4 is not redundant with query 3, and a retry cannot slip past
either.** Every *scheduled* invocation goes through `run_scheduled_pull`, which
writes its `IntegrationRun` row before doing anything else — so query 3 counts
beats, manual triggers and retries alike, at the run level rather than the
schedule level. `pull_crm_tickets` declares no `autoretry_for`, so a failed
poll does not reschedule itself, but a retry introduced later would still
appear as a run.

The second task is the gap. `app.tasks.crm_ticket_pull.sync_crm_ticket` is
webhook-driven and calls `sync_ticket_by_id` **directly**, never touching
`run_scheduled_pull` — so it can write a `support_tickets` row while leaving no
`IntegrationRun` behind at all. Query 3 cannot see it. That is precisely why
the observation also checks the destination: **do not drop query 4 as
duplicative of query 3.** Disabling the scheduler control does not disable this
task either; only revoking the webhook transport does, which is containment
item 4.

**Note what the deploy gate will not tell you.**
`scripts/integrations/verify_crm_ticket_readiness` makes **zero** network
calls. It keeps passing against a dead host until the rows are removed, so it
is not evidence about the poller and must not be cited as any.

## Steps 4-6: what the removal touches, including six things that bite

`crm_ticket_pull` appears in 37 tracked files. Most are ordinary. These are
the ones that fail late, in CI, after the interesting work looks finished.

| # | Where | What happens |
|---|---|---|
| 1 | `tests/architecture/test_communication_eligibility_ownership.py` — `LEDGER_BYPASS_BACKLOG` | `app/services/crm_ticket_pull.py` is listed. Removal **lowers** the count and trips the ratchet's shrink direction, which fails until the baseline is lowered in the same change. Working as designed — a two-directional ratchet is supposed to notice. |
| 2 | `scripts/integrations/verify_crm_ticket_readiness.py` (via `scripts/deploy.sh:981`) | Zero network calls, so it passes against a dead host. Pinned by `tests/architecture/test_integration_platform_boundary.py:177-182`, so removing the deploy step requires updating that test too. |
| 3 | `tests/architecture/test_support_ticket_sot_boundary.py:68` | Asserts `"is_internal=True" in _source("app/services/crm_ticket_pull.py")` — **a guard that reads the file being deleted.** It does not fail with a helpful message; it fails on a missing path. |
| 4 | `app/services/task_reliability.py:215-216` | Both tasks carry contract entries. Delete the tasks without these and the registry keeps names nothing implements. This is precisely the "task vocabulary" half of step 6's ratchet. |
| 5 | `tests/architecture/test_ci_pipeline.py:134` and `scripts/ci/classify_postgresql_changes.py:140` | Both name `tests.test_crm_ticket_pull` as a **helper module** in the PostgreSQL change classifier. Deleting that test module changes how CI classifies unrelated future changes. |
| 6 | `.dotmac/standards-profile.json:111` and `docs/external-connector-surface.md:115` | **Governance-owned surfaces.** The profile lists `tests/test_crm_ticket_pull.py` in its contract surface and is validated by the `Dotmac engineering standards` job against the pinned Governance revision; the surface document records a **content digest** (`03c45fad…`) for `test_latest_crm_updated_at_watermark` under the schema-9 external-connector ratchet. Per `AGENTS.md` that ratchet is Governance-owned and transitional — Sub records measured debt there and never copies the detector. Deleting the test changes both. **Confirm the Governance side before starting**, or the removal stalls at the last gate. |

Two further notes. `alembic/versions/309_retire_feature_aliases.py:171`
references the `crm.ticket_pull` alias and is an **applied migration** — it is
history and must not be edited. And there are **two** settings, not one:
`crm_ticket_pull_enabled` and `crm_ticket_pull_interval_minutes`
(`app/services/settings_seed.py:1384`), plus the control-registry entry and
its `LegacyAlias` in `app/services/control_registry.py:441`.

## Step 6: what the ratchets must cover

File paths alone will not hold this. A returning poller would reappear as a
scheduler entry, a Celery task name and a control key — three vocabularies,
none of which is a file path, and exactly the class ADR 0018 rule 2 names when
it says a source-level sweep cannot see a schedule or a flag. Ratchet each
independently, in both directions:

- **Scheduler** — no `crm_ticket_pull*` key may be registered by
  `app/services/scheduler_config.py`.
- **Task** — no `app.tasks.crm_ticket_pull.*` name in the Celery route table,
  `app/services/task_reliability.py`, or `app/tasks/__init__.py`.
- **Config** — no `crm.ticket_pull` control, no `crm_ticket_pull_enabled` or
  `crm_ticket_pull_interval_minutes` setting spec, and no `CRM_TICKET_PULL_*`
  environment alias.

Each needs the sensitivity proof the fleet convention requires: construct the
returning shape and assert the guard fires. A guard over an empty set passes
for the wrong reason.
