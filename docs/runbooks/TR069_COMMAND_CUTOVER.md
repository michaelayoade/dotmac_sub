# TR-069 Command Lifecycle Cutover

Owner: network operations

Applies to: migration `409_tr069_operation_lifecycle`

## Purpose and safety boundary

This is a forward-only authority cutover from executable `tr069_jobs` rows to
the durable network-operation ledger and dispatch outbox. The migration:

- marks pre-cutover queued jobs failed without executing them;
- marks pre-cutover running or pending jobs unverified;
- clears every pre-cutover plaintext payload;
- drops the legacy retry columns and execution controls; and
- installs the permanent command-outcome reconciler identity.

Do not run the migration while the old application, Celery workers, or beat
scheduler can still admit or execute TR-069 jobs. Do not use Alembic downgrade
as a rollback: migration 409 intentionally has no reverse path.

## Preconditions

1. Name and record the target host and maintenance window.
2. Confirm the release commit and immutable image both contain migration 409.
3. Take and verify a recoverable PostgreSQL backup or snapshot.
4. Confirm Alembic has exactly one current head and the database is at
   `408_radius_session_latest_projection`.
5. Record counts only; never export or log `tr069_jobs.payload`,
   `secure_payload`, ACS credentials, or parameter values:

   ```sql
   SELECT status, count(*) FROM tr069_jobs GROUP BY status ORDER BY status;
   SELECT count(*) AS executable_pre_cutover
   FROM tr069_jobs
   WHERE status IN ('queued', 'running', 'pending');
   ```

6. Notify operators that queued commands will not execute and running or
   pending commands will require device-state review after cutover.

## Cutover

1. Stop web/API producers, Celery beat, and all workers that can run the old
   TR-069 task path. Keep them stopped until migration and verification finish.
2. Apply the immutable release's database migrations once.
3. Verify the database reports only head `409_tr069_operation_lifecycle`.
4. Start the application and workers from the same immutable release.
5. Start Celery beat only after the application and ACS worker are healthy.
6. Do not manually replay a pre-cutover command. Submit any reviewed retry as a
   new command after current device state and a fresh Inform are confirmed.

## Verification gate

Run these checks before ending the maintenance window:

```sql
SELECT status, count(*) FROM tr069_jobs GROUP BY status ORDER BY status;

SELECT count(*) AS unlinked_executable_rows
FROM tr069_jobs
WHERE network_operation_id IS NULL
  AND status IN ('queued', 'running', 'pending');

SELECT name, task_name, enabled
FROM scheduled_tasks
WHERE name IN ('tr069_job_executor', 'tr069_command_reconciler')
   OR task_name IN (
       'app.tasks.tr069.execute_pending_jobs',
       'app.tasks.tr069.reconcile_command_outcomes'
   )
ORDER BY name;
```

The gate passes only when:

- `unlinked_executable_rows` is zero;
- `tr069_command_reconciler` points to
  `app.tasks.tr069.reconcile_command_outcomes` and is enabled;
- no executable `tr069_job_executor` schedule remains;
- the durable network-operation dispatch publisher is enabled;
- application, ACS worker, beat, and migration logs contain no migration,
  decryption, dispatch, or reconciliation errors; and
- operators have a reviewed list of pre-cutover failed and unverified jobs.

Admit one low-risk test command only after the above checks pass. Confirm it
creates one linked network operation, dispatch row, and `tr069_jobs`
projection, and that it reaches a terminal state without exposing payload
values in logs, events, or the operator projection.

## Recovery

Before any new TR-069 command is admitted, a failed cutover may be recovered by
stopping all new processes, restoring the verified pre-cutover database
backup, and restoring the previous immutable application image as one unit.

After a new command has been admitted under migration 409, do not restore the
old executable path or replay ambiguous ACS work. Keep producers stopped,
preserve the operation/job evidence, and perform a reviewed fix-forward. Treat
every running, pending, or unverified command as potentially delivered until
current device state proves otherwise.

