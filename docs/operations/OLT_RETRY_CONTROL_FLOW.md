# OLT health retry control flow

`app.tasks.olt_health_retry.retry_single_olt` is the Celery delivery adapter
for the existing OLT ping retry. Its reliability declaration remains in
`app/services/task_reliability.py`; this correction introduces no new business
owner, projection, transaction, schema, or device command.

A scheduled retry raises Celery's `Retry` signal. The adapter must propagate
that signal so the worker records RETRY, not SUCCESS containing an error such
as `Retry in 30s`. The existing maximum retry count, delay, failed-device
selection, ping operation, and exhausted-retry outcome are unchanged.

The 23 September 2026 log export contains one swallowed retry signal at
2026-09-22T21:10:43.697Z. That observation does not establish that the OLT
recovered, or that a later retry was not delivered.

Regression coverage: `tests/test_olt_health_retry_control_flow.py` exercises
retry propagation, exhaustion, recovery, and unrelated errors using the
existing task boundary. These are isolated unit tests, not live network or
PostgreSQL acceptance. Run the repository's required hosted CI before merge.
