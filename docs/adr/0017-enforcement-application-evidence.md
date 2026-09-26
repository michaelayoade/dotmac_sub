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

2. **Canonical writer.** `access.session_enforcement` is the only writer. The
   writer is a private function in `app/services/enforcement.py`. No adapter,
   handler, task or other service writes the model (architecture-tested).

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
   - a private writer inside the owning module;
   - a record the calling transaction never reads back;
   - id-only keys (`subscription_id`, `nas_device_id`) with **no foreign keys**,
     because callers hold `SELECT … FOR UPDATE` on the subscription row
     (e.g. `fup_state.py`, `account_lifecycle.py`) and an FK check from a second
     connection would self-deadlock against that lock.

   This is the third copy of the pattern. A shared out-of-band evidence helper is
   a recorded extraction candidate, not part of this decision.

4. **Typed outcomes.** The per-NAS enforcement functions return a typed
   `EnforcementOutcome` value (applied / not applicable / failed with class and
   sanitized detail) instead of `bool`. Where SSH is tried first and the API is
   the fallback, the final recorded outcome keeps the most specific real failure:
   an SSH failure followed by "no API credentials" stays an SSH failure class,
   not `not_capable`.

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
   the caller could re-trigger effects.

8. **Later slices, same owner.** Stateless `ActionReadiness` projections per NAS
   ("can Sub enforce here?") and per subscription ("is the intended access state
   applied?"); the reconciler that retries failed rows from the authoritative
   intended state (`account_lifecycle`'s access state), never from this record;
   the `access_enforcement` observability signals and critical alert rules; and
   the exceptions queue. `NasDevice.health_status` keeps its single writer (the
   uptime monitor); enforcement capability is a separate projection.

## Invariants

- Exactly one writer of `EnforcementApplication`: `access.session_enforcement`.
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
- New owner and paths: `access.session_enforcement` writes
  `enforcement_applications` from `_enforce_address_list_on_nas` and the API/SSH
  kick paths.
- Backfill/repair: none. The table starts empty; history before the cutover is
  only in logs.
- Shadow or verification phase: slice 1 records without changing behaviour; the
  record is compared against logs and router state before any projection or
  alert depends on it.
- Cutover gate and evidence: Postgres-lane tests prove survival across the
  dispatcher, Celery and lock-compatibility cases.
- Fallback retirement: the warning-only failure logs stay until the readiness
  projection slice lands, then are reduced to structured records.
- Schema contract step: additive table only; no existing column changes.

## Verification

- Postgres-lane integration tests (the SQLite lane shares one connection and
  cannot prove independence):
  - a `subscription_resumed` event through the real `EnforcementHandler` with the
    NAS rejecting authentication and a second step failing: the handler raises,
    and a fresh connection sees the row with `auth_rejected` and
    `attempt_count = 1`;
  - the scheduled cleanup task rolls back and the row survives;
  - with `SELECT … FOR UPDATE` held on the subscription row, the evidence write
    completes within a 200 ms `lock_timeout`.
- Unit: a classifier table test including near-miss cases; outcome mapping for
  applied / not applicable / failed, including the SSH-then-no-API case.
- Architecture: only `app/services/enforcement.py` writes the model, with a
  planted-violation sensitivity proof.
- SOT registry: `access.session_enforcement` declares the observation in `owns`
  and carries a complete `ServiceContract`.

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
