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

## READ THE ROW FIRST — it has been Off since 2026-08-18

Everything below about *how* to turn the control off remains correct, and on
2026-08-30 none of it was needed. Authenticated observation of the system
modules screen:

```
CRM module          Disabled
crm.ticket_pull     stored Off
effective value     Off
owner / source      database row modules.crm_ticket_pull
canonical change    2026-08-18 23:37 WAT
```

Production carries `CRM_TICKET_PULL_ENABLED=true` in its environment. That is
**stale lower-precedence residue, not proof the canonical control is enabled** —
nothing reads it (see the section below, and
`tests/architecture/test_crm_ticket_pull_resolution.py`, which pins that no
path from the environment exists).

**The lesson, recorded because it cost a day.** Two readers established
correctly *which* input governs — the row, not the variable — and neither read
what the row said. Establishing which input is authoritative is not the same as
reading it, and the second step is by far the cheaper one. Read the row before
reasoning about what might be setting it:

```sql
SELECT value_text, is_active, updated_at
  FROM domain_settings
 WHERE domain = CAST('modules' AS settingdomain) AND key = 'crm_ticket_pull';
```

### What this changes about the receipt

The retirement's `effective_time` is **2026-08-18**, when the control was
actually turned off — not the date anyone confirmed it. The attribution is the
`audit_events` row `feature_controls.update` carrying `actor_id` at that
timestamp, written by whoever made the change.

**If that audit row is absent, say so.** Record the attribution as
`unavailable`, with the authenticated observation of 2026-08-30 and the
interval evidence standing in its place.

> **Never toggle the control on and off to manufacture an attributed event.**

That temptation is real, because a clean attributed pair would look like a
better record than an absent one. It would be a fabricated event describing a
state change that did not happen for the reason the record implies, in a
receipt whose entire purpose is to be trustworthy about what happened. An
honest *"disabled 2026-08-18, attribution not recoverable, confirmed by
authenticated observation on 2026-08-30 plus two clean intervals"* is worth
more, and it is the same discipline that put `unavailable` in
`final_sync_watermark` and `runtime_observation` rather than inferring them.

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

### Which route: the adapter, and the reason is attribution not authorization

The canonical writer is `control_registry.update_canonical_feature_controls`.
The admin POST at `app/web/admin/system.py` is **one adapter over it**, not the
owner — so "go through the canonical writer" is, on its face, satisfied by
calling the function inside the running application container. Most of what you
need does come from the service:

| | service called in-container | via the admin adapter |
|---|---|---|
| the state change | yes | yes |
| `validate_feature_control_changes` | yes — called inside the service | yes (also re-checked in the route) |
| before/after stored value, effective value, source | yes — built by the service and returned | yes |
| `domain_setting_history` row | yes — recorded at the MODEL boundary by `app/services/setting_history.py`, so every writer is covered | yes |
| `changed_by_party_id` on that history row | **NULL** | set |
| `audit_events` row `feature_controls.update` with `actor_id` | **not written at all** | written by the adapter |

**The last two rows are why the adapter is the right route here.** The audit
event exists only on the HTTP path, and history attribution comes from
`set_change_context`, which the caller must set explicitly — the history
recorder is an ORM event with no access to the caller, and *nothing guesses*.

For an ordinary toggle that gap is tolerable. For this one it is not. ADR 0018
rule 1 field 5 requires the barrier to write **an attributed record — actor,
timestamp, resource, old owner, new owner** — and the reason that field exists
is the CRM chat cutover, whose barrier "wrote nothing durable. No audit row, no
metric, no counter." Retiring the last CRM writer through an unattributed path
would reproduce, in the act of retirement, the exact defect that made the
previous cutover unanswerable. The receipt would not be able to name who
performed it.

**If the adapter is genuinely unavailable** and the change must be made
in-container, it can still be attributed — but only deliberately:

```python
from app.services.setting_history import (
    SettingChangeContext, set_change_context, reset_change_context,
)

token = set_change_context(SettingChangeContext(
    actor_party_id=<operator party id or None>,
    reason="CRM ticket-pull retirement, containment item 2",
    request_id=<change ticket reference>,
))
try:
    changes = control_registry.update_canonical_feature_controls(
        db, payload={"crm.ticket_pull": False}
    )
finally:
    reset_change_context(token)
```

That fills `domain_setting_history`, and `changes` is the receipt material.
It still does **not** write the `feature_controls.update` audit event, so
record that omission explicitly on the receipt rather than letting the gap pass
unmentioned. An in-container change made *without* the context block is an
unattributed production change and must not be used for this retirement.

### Who can perform it: a named human operator, and there is no service principal

Searched, so nobody repeats it:

- `system:settings:read` / `system:settings:write` are RBAC permissions seeded
  by `scripts/seed/seed_rbac.py` onto **staff roles**. They are held by system
  users who authenticate with a `UserCredential`.
- The route is `POST /admin/system/modules`, form-driven, guarded by
  `require_permission("system:settings:write")` → `require_user_auth`, and
  `_system_actor_id(request)` stamps that authenticated principal into the
  audit row.
- **Every OpenBao pointer in this repository is application material**, not a
  portal login: `secret/settings/auth`, `secret/settings/crypto`,
  `secret/settings/machine_auth`, `secret/database`, `secret/redis`,
  `secret/s3`, `secret/paystack`, `secret/radius`, `secret/notifications`,
  `secret/genieacs`, `secret/integrations/meta_social`.
  `secret/settings/machine_auth` is the **HMAC key**
  (`machine_credential_hmac_key`) that `dotmac_kernel.machine_auth` uses to
  hash integration-platform machine credentials — not an admin credential and
  not a principal.

**There is no service or automation principal holding `system:settings:*`.**
This step is performed by a named human operator signing in as themselves.

That is a property of the design, not a gap to route around, and it follows
from what the audit row is *for*. The row records **who engaged the barrier**.
Performing the change under someone else's credential would produce a row that
is attributed and **wrong** — authoritative-looking, and naming a person who
did not make the change. That is worse than an unattributed change, not better,
and it would corrupt the very field the receipt depends on. Do not borrow a
credential to satisfy the attribution requirement; the requirement is that the
attribution be *true*.

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

## There are TWO schedule entries, and one of them is daily

`build_beat_schedule()` registers **both** of these behind the single
`crm_ticket_readiness.schedule_enabled` gate
(`app/services/scheduler_config.py:2125-2136`):

| Entry | Cadence | Task |
|---|---|---|
| `crm_ticket_pull` | every `crm_ticket_pull_interval_minutes` (default 5) | `pull_crm_tickets` |
| `crm_ticket_pull_full` | **daily, `crontab(hour=3, minute=40)`** | `pull_crm_tickets(full=True)` |

Turning the control off removes both, because both sit behind the same gate.
But **two five-minute intervals do not observe the daily entry at all** — it
simply was not due. A receipt claiming "no schedule execution" on the strength
of a ten-minute window has said nothing about `crm_ticket_pull_full`, and
saying so is the difference between an observation and an assumption.

### Prove the schedule is gone instead of waiting a day for it

Do not extend the window to 24 hours. The schedule is *rendered*, so read it:

```
# On the app host, against the production database, read-only.
python -c "from app.services.scheduler_config import build_beat_schedule; \
           s = build_beat_schedule(); \
           print([k for k in s if 'crm' in k])"
# Expect []. Before the flip, expect ['crm_ticket_pull', 'crm_ticket_pull_full'].
```

`build_beat_schedule()` is the single builder — `app/celery_app.py:51` sets it
at boot and `app/celery_scheduler.py:86` reloads it. An empty list is direct
evidence that neither entry can fire again, and it is available the moment the
control flips rather than after a day. Run it **before** the flip too: that is
the positive control for this instrument, exactly as query 1 is for the run
ledger.

## Item 4: the revocation inventory, and the part this repository cannot close

`old_writer_retirement` cannot move to RETIRED until each of these has an
explicit disposition. Sub can name the first three precisely. It cannot name
the fourth at all.

| Target | Where it lives | Disposition |
|---|---|---|
| **Control** | `domain_settings`, `domain='modules'`, `key='crm_ticket_pull'` | set false via `update_canonical_feature_controls` |
| **Capability binding** | `integration_capability_bindings`, `capability_id='crm.ticket_observation.v1'`, `state='enabled'` | disable — the second kill switch |
| **Installation** | `integration_installations`, `connector_key='dotmac.crm'`, `state='enabled'` | disable — this is what retires the transport |
| **Job** | `integration_jobs` bound to that binding, `is_active` | deactivate |
| **Transport / DNS** | `integration_config_revisions.config_json ->> 'base_url'` on that installation — the dead `crm.dotmac.io` host | the revision rows are append-only history; disabling the installation is what stops them being used. Do **not** rewrite historical revisions |
| **Credential** | `integration_config_revisions.secret_refs ->> 'service_credentials'` — a **pointer**, resolved only inside connection validation | revoke at the store the pointer names. Record the pointer, never the value |
| **Webhook transport** | the `sync_crm_ticket` path — see the observation section | **turning the scheduler control off does not disable this.** Only revoking the webhook transport does |
| **Monitoring binding** | **not in this repository** | see below |

### The monitoring binding cannot be closed from Sub

There is no Prometheus, alert-rule, blackbox or scrape configuration anywhere
in this repository — a search of `deploy/`, `docker/`, `config/` and `nginx/`
for CRM references returns nothing. That surface belongs to Observer, in
Observer's own repository and on Observer's host.

This matters more than it looks. A scrape or alert still pointed at a retired
transport keeps the dependency alive in Observer's view of the world, so the
decommission claim would be contradicted by the monitoring system itself. Under
ADR 0018 rule 2 that item still needs a disposition, and it cannot be given one
here: **carry it as an open STILL LIVE entry owned by Observer's lane** until
Observer records its retirement. Closing this runbook's other items does not
close that one, and a receipt that quietly omits it is the "absence is never a
disposition" failure the rule names.

## Evidence each live change must produce

Per change, not per session — the sequence is only as good as its weakest
recorded step:

1. **Snapshot.** The exact prior state: the control row's `value_text` and
   `is_active`; the installation, binding and job states; the rendered beat
   schedule keys; the run-ledger cadence from query 1.
2. **Apply**, through the canonical owner only. Keep the writer's returned
   before/after record — `update_canonical_feature_controls` returns stored
   value, effective value and source for each key it changed.
3. **Re-observe.** Queries 3 and 4, plus the rendered-schedule check, plus the
   readiness issue codes.
4. **Rollback path**, written down before applying, and reachable by the same
   canonical owner rather than by direct SQL.
5. **Exact restoration evidence** if rolled back: the same snapshot fields,
   re-read, matching.

If any one of those cannot be produced, stop and report rather than continuing.
A half-executed revocation across a control, an installation, a credential
store, DNS and a monitoring system is worse than none: it leaves a dependency
alive while the receipt says it is gone, which is precisely the failure ADR
0018 exists to prevent.

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
