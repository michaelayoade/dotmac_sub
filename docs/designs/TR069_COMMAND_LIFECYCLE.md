# TR-069 Command Lifecycle

Status: complete authority cutover

Owner: `network.tr069_commands`

## Decision

`network.tr069_commands` is the only application owner that may admit,
execute, or classify the outcome of a TR-069 CPE command. The network operation
ledger owns the durable lifecycle decision, the network operation dispatch
outbox owns broker delivery, GenieACS is an observation source, and
`tr069_jobs` is a read-only operator projection.

`network.tr069_command_admission` controls only acceptance of new work.
Accepted work is drained permanently. Disabling admission never pauses
dispatch publication, execution claims, or outcome reconciliation.

There is no executable compatibility path. Migration
`409_tr069_operation_lifecycle` fails pre-cutover queued work without executing
it, marks pre-cutover running or pending work `unverified`, clears every
pre-cutover payload, and removes the old control identity.

The operator procedure, verification gate, and forward-only recovery boundary
are defined in `docs/runbooks/TR069_COMMAND_CUTOVER.md`.

## Ten risks closed by this cutover

1. **A mutable execution flag could strand accepted work.** The new capability
   is admission-only; dispatch publication and lifecycle reconciliation are
   permanent scheduler responsibilities.
2. **Job persistence and task publication were not atomic.** Admission now
   commits the operation, encrypted payload, job projection, event, and typed
   dispatch row in one owner transaction.
3. **Duplicate or overlapping requests could issue competing device
   commands.** Exact active duplicates replay the same operation using a
   device, command-kind, and payload fingerprint. A device lock and active
   device-operation check reject a different command until the first reaches
   terminal state.
4. **Duplicate broker envelopes or existing ACS work could race.** The
   dispatch owner claims one envelope, the command owner permits only a locked
   `queued -> running` transition, and an ACS pending-work preflight blocks a
   new submission instead of reusing or replacing someone else’s task.
5. **Automatic retry could repeat a destructive command.** ACS submission has
   a no-retry contract. A timeout, interrupted worker, or missing confirmation
   becomes `unverified` and requires current-state review.
6. **ACS acceptance was treated as device success.** Accepted task identifiers
   project `pending`; success requires later task absence without a recorded
   fault plus a device Inform newer than task acceptance.
7. **Missing device or ACS prerequisites could leave a queued row forever.**
   The execution claim terminalizes the operation and projection as failed in
   the owning transaction.
8. **Generic CRUD could rewrite or erase history.** The old create, update,
   delete, execute, and cancel methods are removed. The remaining job service
   is read-only, and the obsolete retry counters are dropped.
9. **Wi-Fi values, firmware URLs, or embedded credentials could persist in
   plaintext projections.** Strict typed validation rejects credential-bearing
   URLs; sensitive execution payloads use `EncryptedJSON`; public projections
   and events are redacted; terminal state clears the encrypted payload.
10. **The non-durable bulk envelope and pre-cutover rows could preserve a
    parallel authority.** Bulk fan-out admits each device through the same
    owner and reports partial acceptance explicitly. Migration terminalizes
    every executable pre-cutover row, and no runtime adoption path remains.

## State machine

The canonical operation and job projection move together:

```text
admission
  operation: pending
  job:       queued
       |
       | durable dispatch claim + locked execution claim
       v
  operation: running
  job:       running
       |
       +-- ACS task id ----------> operation: waiting / job: pending
       |                              |
       |                              +-- absent, no fault,
       |                              |   newer Inform ------> succeeded
       |                              +-- recorded fault ----> failed
       |                              +-- timeout ------------> unverified
       |
       +-- confirmed rejection ----> failed
       +-- ambiguous/interrupted --> unverified
```

Terminal states never regress. A new attempt after `failed` or `unverified`
must be a new command and operation; no worker automatically replays the
previous side effect. After `unverified`, admission remains closed until the
device supplies an Inform newer than the ambiguous operation, providing a
fresh observation boundary before another command can be accepted.

## Secret and evidence contract

- `Tr069Job.secure_payload` is the only persisted execution payload and uses
  the repository encryption-at-rest type.
- `Tr069Job.payload` contains only paths, counts, filenames, and redaction
  markers.
- Events contain lifecycle identity and status, never parameter values,
  firmware URLs, passwords, or tokens.
- `external_task_ids`, `submitted_at`, and `last_observed_at` preserve the
  evidence used to classify a pending command and prove a newer Inform.
- Terminal transitions clear `secure_payload`.
- `app.tasks.tr069.reconcile_command_outcomes` is the permanent, idempotent
  repair path for pending or interrupted delivery.
- Generic job and operation retention does not delete this lifecycle evidence;
  the command owner remains responsible for any future reviewed retention
  policy.

## Verification gate

The cutover is complete only when focused tests prove atomic admission,
duplicate replay, admission-disable semantics, single execution claim,
missing-prerequisite terminalization, pending/fault/success classification,
ambiguous-delivery handling, permanent scheduler protection, secret redaction,
migration terminalization, and absence of old producers.
