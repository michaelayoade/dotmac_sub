# ADR 0017: Enforcement application evidence is a durable, owner-written observation

Status: proposed

Date: 2026-09-26

Decision owner: Michael

Affected systems and domains: network access control plane
(`access.session_enforcement`), subscription enforcement (address-list block
and unblock, API/SSH session kick), event dispatch, scheduled enforcement,
FUP lift, admin readiness projections (later slices), observability.

## Context

On 2026-09-17 the Eagle FM Access router was replaced. The replacement's
configuration was restored from an export, which carries no user passwords,
so Sub's RouterOS API user no longer existed. From 2026-09-17 09:54 UTC until
2026-09-25 every address-list suspend/restore on that router failed.

Nothing in Sub recorded it. `_enforce_address_list_on_nas` and the kick paths
in `app/services/enforcement.py` swallow every failure into `return False`
plus a `warning` log, and the callers (`apply_subscription_address_list_block`,
`remove_subscription_address_list_block`) only return a count. So:

- no persisted state said "this subscription should be blocked but is not";
- `NasDevice.health_status` (written by the uptime monitor from ping/SNMP)
  stayed healthy;
- the enforcement event handler never failed, so no retry or review item;
- GlitchTip only captures `ERROR` records, so it never saw the warnings;
- no alert existed.

The same `return False` also conflates "not applicable" (not MikroTik, no
credentials, feature disabled) with real failures.

No existing record owns this fact. `EnforcementLock` is decision state with no
NAS dimension; `FupState` is FUP runtime state; `ProvisioningLog` belongs to
template runs and NAS auth probes; `NetworkOperation` tracks different effects;
the in-process CoA negative cache and the poller's reachability state are not
durable per-subscription records.

The obvious write location does not survive the failure case:

- the event dispatcher runs each handler in an isolated savepoint session
  (`app/services/events/dispatcher.py`) that rolls back when the handler raises,
  and the enforcement handler raises precisely when a step fails;
- `enforcement_scheduled.cleanup_subscription_block_sessions` rolls back on any
  exception;
- `lift_fup_enforcement` removes the address-list block inside FUP owner
  commands that roll back as a unit;
- `execute_owner_savepoint` is a `RELEASE SAVEPOINT`, which does not survive an
  outer rollback.

A router change is irreversible regardless of what the database transaction
does afterwards, so evidence of it must not share that transaction's fate.

## Decision

1. **Authoritative record.** A new observation, `EnforcementApplication`, holds
   one current-state row per (subscription, NAS device, effect), where effect is
   `address_list_block`, `address_list_unblock` or `session_kick` in this slice.
   It records the outcome (`applied`, `failed`, `not_applicable`), a typed
   failure class (`auth_rejected`, `unreachable`, `timeout`, `command_failed`,
   `not_capable`), a sanitized detail, `attempt_count`, `first_failed_at`,
   `last_attempt_at`, `last_success_at` and the path used (`ssh`, `api`).
   It is an observation: a fact about what happened on a device, not a decision.

   **Registry status.** The observation has its own contracted service,
   `access.enforcement_evidence` (`app/services/enforcement_evidence.py`), in
   transaction mode `out_of_band_evidence`. That mode is added to the SOT
   manifest by this decision and validated narrowly: observation collectors
   only, an approving ADR cited in `design_refs`, and no event contract; within
   that shape it needs no domain error codes or events, because the writer never
   raises into its caller and emits nothing
   (`tests/architecture/test_sot_manifest_out_of_band_evidence.py`).
   `access.session_enforcement` performs the attempts and depends on it. Its own
   CoA, session-closure and recovery concerns remain on the shrink-only legacy
   manifest baseline as pre-existing debt, independent of this record.
   `app/services/enforcement.py` is also registered under `sessions.enforcement`;
   which name owns `update_subscription_sessions` is settled when that service
   is contracted.

2. **Canonical writer.** `access.enforcement_evidence` is the only writer,
   through `record_enforcement_application` in
   `app/services/enforcement_evidence.py`. `access.session_enforcement` calls it
   with each final per-NAS outcome. No adapter, handler, task or other service
   writes the model (architecture-tested).

3. **Approved exception: out-of-band evidence write.** The writer opens its own
   short unit of work with `db_session_adapter.create_session()`, upserts the row
   (`INSERT … ON CONFLICT (subscription_id, nas_device_id, effect) DO UPDATE`),
   commits, and closes. This is an explicit, narrow exception to the AGENTS.md
   rule that nested helpers never commit independently. It follows the existing
   precedents `_record_review_item_out_of_band`
   (`app/services/prepaid_service_renewals.py`) and
   `_record_mikrotik_auth_attempt` (`app/services/nas/_mikrotik.py`). The
   exception is limited to:
   - evidence of an external, irreversible effect or its failure;
   - one writer function inside the owning module;
   - a record the calling transaction never reads back;
   - id-only keys (`subscription_id`, `nas_device_id`) with **no foreign keys**,
     because callers hold `SELECT … FOR UPDATE` on the subscription row
     (e.g. `fup_state.py`, `account_lifecycle.py`) and an FK check from a second
     connection would self-deadlock against that lock.

   This is the third copy of the pattern. A shared out-of-band evidence helper is
   a recorded extraction candidate, not part of this decision.

4. **Typed outcomes, final per NAS.** The per-NAS enforcement paths compute a
   typed `EnforcementOutcome` (applied / not applicable / failed with class and
   sanitized detail) instead of a bare `bool`, and record ONE final outcome per
   (subscription, NAS, effect) across their tiers:
   - address lists (SSH, then API): an SSH failure followed by "no API
     credentials" stays the SSH failure, not `not_capable`;
   - session kick (API, then SSH): a later tier's success supersedes an earlier
     failure; when the SSH tier also fails it keeps the earlier classified cause
     (e.g. `auth_rejected`), because the SSH helpers swallow their exceptions; and
     when the SSH tier cannot run at all (no username, non-MikroTik, no SSH
     credentials, or `access.mikrotik_session_kill` disabled) it is
     configuration absence, not a failure (`_ssh_kick_outcome`,
     `_ssh_kick_possible`);
   - `applied` requires confirmation: the RouterOS API kick's read-back returns
     only the sessions it confirmed gone, so an empty or partial result with no
     exception is `failed`/`command_failed` (`session_kick_unconfirmed n/m`);
   - configuration absence is `not_applicable`, never a failure: no SSH
     credentials or management IP (the exact pre-connection refusals of
     `DeviceProvisioner.ssh_session`), a non-MikroTik NAS, or no transport;
   - a `not_applicable` outcome clears the row's failure streak
     (`attempt_count`, `first_failed_at`).

5. **One classifier.** Failure classification lives once in the NAS transport
   layer (`app/services/nas/`). It matches exception types first (routeros_api,
   paramiko, socket/OSError) and falls back to message text. Its detail always
   passes through `app.logging.sanitize_exception`.

6. **Behaviour in this slice is recording only.** Enforcement results, counts,
   handler success/failure and retries are unchanged. A failure is recorded, not
   raised.

7. **Evidence-write failure.** If the out-of-band write itself fails, the writer
   logs at `ERROR` (so GlitchTip opens an issue) and continues. It never raises
   into the caller, because the device effect has already happened and failing
   the caller could re-trigger effects. The one exception is Celery's
   `SoftTimeLimitExceeded`, which is re-raised so a task cannot run past its
   budget. Known bound: waiting for a pooled connection is limited by the pool
   timeout, not by `lock_timeout`; under pool saturation the caller can wait up
   to that timeout while holding its own locks, and the failure is then logged.

8. **Later slices, same owner.** Stateless `ActionReadiness` projections per NAS
   ("can Sub enforce here?") and per subscription ("is the intended access state
   applied?"); the reconciler that retries failed rows from the authoritative
   intended state (`account_lifecycle`'s access state), never from this record;
   the `access_enforcement` observability signals and critical alert rules; and
   the exceptions queue. `NasDevice.health_status` keeps its single writer (the
   uptime monitor); enforcement capability is a separate projection.

## Invariants

- Exactly one writer of `EnforcementApplication`: `access.enforcement_evidence`.
- A recorded row survives a rollback of the transaction that performed the
  enforcement attempt.
- Writing evidence never blocks on, or deadlocks against, the caller's
  subscription row lock.
- `not_applicable` is never recorded as a failure, and a real failure is never
  recorded as `not_applicable`.
- Recorded details never contain credentials (sanitized via `app.logging`).
- The record is evidence, not authority: no decision or projection reads it as
  the intended state.

## Consequences

- Operators and later slices get a queryable record of every enforcement attempt
  per subscription, router and effect, including first failure and last success.
- One extra short database transaction per recorded enforcement attempt on a
  separate connection. Enforcement is per-subscription and low-volume relative to
  the pool.
- A row can exist for a subscription or NAS that is later deleted (no FKs).
  Readers must tolerate dangling ids; a cleanup policy belongs to the reconciler
  slice.
- Rejected alternatives:
  - *Same session as the enforcement call*: the evidence is lost in exactly the
    failure case (handler, Celery and FUP paths all roll back).
  - *Record in the dispatcher/task adapter* (like `record_handler_attempt`):
    creates three writers and puts adapters in charge of a domain fact; a raised
    exception also carries no typed outcome back to the adapter.
  - *`execute_owner_savepoint`*: a savepoint release does not survive an outer
    rollback and requires an active owner command.

## Migration and cutover

- Old owner and paths: none. Failures were log-only.
- New owner and paths: `access.enforcement_evidence` writes
  `enforcement_applications` for `access.session_enforcement`, from `_enforce_address_list_on_nas` (address-list
  block/unblock), the suspend/cancel kick in `disconnect_subscription_sessions`
  and the profile-refresh kick in `update_subscription_sessions` (API then SSH,
  one final outcome per NAS).
- Backfill/repair: none. The table starts empty; history before the cutover is
  only in logs.
- Shadow or verification phase: slice 1 records without changing behaviour; the
  record is compared against logs and router state before any projection or
  alert depends on it.
- Cutover gate and evidence: Postgres-lane proof that the evidence survives a
  rollback of the caller's transaction around the real per-NAS helper, through
  the real `EnforcementHandler` raising `EnforcementProjectionError`, and through
  the real scheduled cleanup task rolling back; and that the write never waits
  on the caller's subscription lock. The record's owner is contracted, so slice
  2 may read it.
- Fallback retirement: the warning-only failure logs stay until the readiness
  projection slice lands, then are reduced to structured records.
- Schema contract step: additive table only; no existing column changes.

## Verification

- Postgres-lane integration tests (the SQLite lane shares one connection and
  cannot prove independence):
  - slice 1 (present): the real `_enforce_address_list_on_nas` runs inside a
    transaction that is rolled back while the API raises the RouterOS login
    rejection; a fresh connection sees the row with `auth_rejected`,
    `attempt_count = 1` and the secret redacted;
  - slice 1 (present): with `SELECT … FOR UPDATE` held on the subscription row,
    the evidence write completes promptly (the writer sets a 2 s `lock_timeout`;
    the test requires completion in under 1.5 s);
  - slice 1 (present): a `subscription_resumed` event through the real
    `EnforcementHandler` with the NAS rejecting the API login and a second
    restore step failing, so the handler raises and its session rolls back; and
    the real `cleanup_subscription_block_sessions` task rolling back after a
    failed commit. The row survives both.
- Unit: a classifier table test including near-miss cases; outcome mapping for
  applied / not applicable / failed, including the SSH-then-no-API case, the
  unconfirmed API kick, and the no-SSH-credentials case.
- Architecture: only `app/services/enforcement_evidence.py` writes the model,
  with a planted-violation sensitivity proof; the `out_of_band_evidence`
  manifest rules have their own positive and negative tests.
- SOT registry: `access.enforcement_evidence` carries a complete
  `ServiceContract` (mode `out_of_band_evidence`) and every registered contract
  validates; `access.session_enforcement` is unchanged on the legacy baseline
  (no new baseline entry).

## Rollback or forward-fix

The table is additive. Reverting the code stops new rows; the table can remain
or be dropped by a follow-up migration. Router effects are never reversed by
this change because it does not alter enforcement behaviour.

## Review and retirement

- Review date: when the readiness-projection slice lands, or 2026-12-31.
- Retirement condition: superseded if a shared out-of-band evidence helper is
  extracted, or if enforcement moves to a durable effect outbox that records
  outcomes itself.
- Supersedes or is superseded by: none.
