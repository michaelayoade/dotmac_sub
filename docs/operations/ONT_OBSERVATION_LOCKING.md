# ONT observation single-flight locking

The scheduled ONT signal snapshot uses the existing
`app.tasks._postgres_lock.postgres_session_advisory_lock` infrastructure
boundary. Its stable key remains `70420615`. The helper pins one PostgreSQL
connection until it can prove that the session-level lock was released;
otherwise it invalidates that connection instead of returning a held lock to
the pool. A failed acquisition skips without opening a snapshot work session.

The snapshot keeps its existing independent transaction, active-ONT cohort,
append-only observation fields, task name, time limits, and reporting keys.
It closes the work session before releasing the lock. This change does not
introduce a domain owner, change the topology interpretation of observations,
add transaction writers, or migrate the existing adapter-transaction debt.
The infrastructure helper is reused rather than maintaining another lock
implementation. No schema change or device operation is involved.

A `recorded` result means the snapshot commit completed. An `error` result
means it did not; Celery transport completion must not be read as business
success. A lock skip alone does not prove whether an earlier run is healthy.
Existing stranded locks require separate operator evidence and are not
released by an unreviewed production repair in this change.

`tests/test_ont_observation_lock_pinning.py` covers lock contention, snapshot
mapping, commit/close/unlock order, soft-limit and ordinary failure cleanup,
and pinned-helper connection invalidation. These are mocked unit tests, not
migrated PostgreSQL concurrency acceptance. The required hosted repository CI
must pass before merge.
