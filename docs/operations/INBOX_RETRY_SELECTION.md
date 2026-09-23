# Failed Inbox retry selection

The existing typed `RetryFailedOutboundCommand` and `MaintenanceOutcome` on
`communications.team_inbox_maintenance` remain the public scheduled-command
boundary. The owner still enters `execute_owner_command` once; its existing
`team_inbox_operations` participant and the outbound owner remain flush-only.
No owner, authority, public outcome, transaction boundary, or schema changes.

The private automatic work query now applies the persisted retry budget before
its batch limit. The newest eligible messages retain priority, with message ID
as a deterministic tie-breaker. A newest page full of exhausted failures can
therefore no longer hide older eligible messages indefinitely. The caller's
retry limit remains in force and is rechecked before the existing outbound
retry command. Existing delivery policy, notification/outbox behavior, and
retry evidence are not bypassed or rewritten.

Missing or null legacy counters retain the existing initial budget. Persisted
counters must otherwise be non-negative integral decimal values representable
in at most 18 digits. Malformed, boolean, fractional, negative, or oversized
values fail closed and remain visible for reviewed correction; the guarded SQL
cast cannot turn them into a fresh retry budget or abort the whole sweep.

`list_failed_outbound_messages` and failure-count projections are unchanged:
exhausted and malformed rows remain failed, visible, and auditable. The task's
`retried` and `skipped` values describe its selected eligible cohort, not every
failed message in the database. A policy rejection remains skipped/failed, not
successful delivery. No automatic reset or historical resend is introduced.

Tests in `tests/test_inbox_retry_eligible_batching.py` exercise the real typed
maintenance command and outbound retry evidence, mocking only delivery. They
cover a full exhausted page, repeat sweeps, limits, counter validation, failure
visibility and rejected-delivery accounting. The integration-marked case must
run on the repository's migration-prepared PostgreSQL fixture, never SQLite.
All other cases are fast unit coverage and are not database-parity acceptance.
