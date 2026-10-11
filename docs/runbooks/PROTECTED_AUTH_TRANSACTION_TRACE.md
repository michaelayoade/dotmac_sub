# Protected auth transaction trace foundation

This is bounded CI/unit test evidence for S12/A-PR2, based on the reviewed
password/portal owner stack at `7038548b8985a2f13b852a196f392a7d0b50962f`.
It is not acceptance of the 33 grouped inventory paths or global audit coverage.
No runtime security behavior or failed-authentication implementation changes.

`tests/support/tx_trace.py` binds Session hooks and one engine listener only
inside `trace_transactions(engine)`. Enter after fixture setup and leave before
verification queries. `finally` removes every installed listener. Session roots
are distinct logical owner transactions, even when the SQLite fixture joins
its isolation transaction using savepoints. Same-engine helper Sessions remain
separate roots. Nested savepoints keep their parent root, nested commits are
not root commits, and rolled-back nested writes cannot supply audit evidence.
Commit hooks run before application after-commit callbacks to preserve ordering.

Immutable results retain sequence, transaction ancestry, DML operation and table
names only. They retain no SQL, parameters, password, tokens, identity values or
mapped payloads. Mapper flush observations supplement engine DML observations;
successful `after_cursor_execute` observations supply SQL write evidence; failed
execution attempts never count. Canonical audit and intent proof require ordinary
VALUES INSERT evidence. Conflict/ignore/replace forms and zero-row non-RETURNING
inserts cannot supply proof. Ordinary VALUES INSERT RETURNING remains supported:
SQLite reports zero before fetching returned rows. This exception requires a
compiled SQLAlchemy VALUES Insert with a real compiler RETURNING clause; raw SQL
literal tokens never grant it and raw zero-row inserts fail closed. Trigger-dependent behavior is
outside this unit foundation. Only qualifying `INSERT audit_events` is canonical
audit evidence. `event_store`,
notifications and intent rows are visible but do not substitute for audit
(Q-A4 remains open). The pinned protected tables are `user_credentials` and
`sessions` (the actual AuthSession table).

`assert_protected_tx` requires protected SQL writes and canonical audit INSERT
in the same committed root, exactly one successful root commit, and no transport
call before its named intent root commits or inside a protected transaction.
An external effect also requires an actual `event_store` INSERT in that root
before its commit; merely naming a previously committed root never supplies
intent evidence. This foundation pins EventStore as the only external intent
table, while canonical audit remains `audit_events`. Transport spies bind the
root for the exact intent exercised; payload/recipient semantics are outside
this table-level foundation. A root supplying audit or intent evidence must not
also UPDATE or DELETE that evidence table. This conservative rule rejects even
unrelated row updates in the same root, because the trace retains no row IDs and
cannot safely distinguish an inserted record later removed or changed.
`success=False` requires rollback with no committed partial records. Transport
coverage requires a bounded spy explicitly calling `record_external_effect`;
no global network-library interception or comprehensive transport coverage is
claimed. Cache projection work is separately owned, rather than audit proof.

## First exemplars and counterexamples

| Test | Evidence and boundary |
| --- | --- |
| `test_full_login_stamps_last_login_and_stages_success_audit` | Real local-password owner success, including Core credential gate, session and canonical audit in one root. |
| `test_audit_failure_inside_phase_b_fails_closed` | Real owner audit failure rolls back gate/session with no committed partial record. |
| `test_reset_owner_transaction_trace_before_delivery` | Real reset owner with real transactional EventStore staging; only `dispatch_after_commit` disabled at its existing emit seam. Proves owner credential/session change plus audit, excluding dispatcher delivery. |
| `test_completion_changes_credential_revokes_sessions_and_spends_token` | Full reset invocation remains a counterexample to the one-root assertion: synchronous post-commit dispatcher/helper Sessions commit additional roots. Existing projection, reset and replay assertions remain. This entire path is not marked atomic/complete. |
| `tests/test_tx_trace.py` | Actual auth/audit SQL counterexamples for missing/separate-session audit, helper commits, two audited roots, EventStore-only evidence, rollback, raw Core UPDATE, nested savepoints, bounded external ordering, failed/no-op INSERT attempts, removed/changed evidence, ordinary INSERT RETURNING and hook cleanup. |

The parser recognizes anchored INSERT INTO / UPDATE / DELETE FROM with simple
optional quoted schema/table names. SELECT (including set_config), transaction
control, tenant-initialization fixture work outside the span and non-DML SQL are
ignored. CTE DML, procedures, triggers, multi-bind Sessions and concurrent use
of a shared connection are outside this foundation; add focused parser and
attribution tests before claiming them. Direct engine DML without a Session
root fails as unattributed. This helper should not be nested on the same engine.

Run the three explicit unit files serially with the repository test environment:

```sh
python -m pytest -q tests/test_tx_trace.py tests/test_password_authentication.py tests/test_credential_recovery.py
ruff check tests/support/tx_trace.py tests/test_tx_trace.py tests/test_password_authentication.py tests/test_credential_recovery.py
ruff format --check tests/support/tx_trace.py tests/test_tx_trace.py tests/test_password_authentication.py tests/test_credential_recovery.py
```

SQLite/model metadata is the non-authoritative fast unit lane. No PostgreSQL
locking, trigger, migration, constraint or deployed-schema acceptance is
provided; the existing migrated PostgreSQL race shard remains authoritative.
No PostgreSQL target was assigned for this foundation. A per-path shrink-only
inventory baseline is deferred until its rows have the required reviewed trace
evidence; these two bounded owner exemplars do not remove inventory debt.
