# Regional report index recovery

## Intent and ownership

PR #3478 bounded regional reports to 366 days, shared one materialized spatial
assignment CTE across all metric groups, and added two billing-period indexes.
PR #3479 consolidated it and renumbered its migration from 654 to 658.
The report remains the read-only `ui.regional_performance_report` projection;
Alembic owns its physical indexes. Neither report logic nor financial records
change in this repair.

`scripts/migration/regional_report_billing_indexes.py` declares the two exact
contracts shared by revisions 658/662 and the read-only deploy schema gate:

| Public table | Index | Ordered keys |
| --- | --- | --- |
| invoices | ix_invoices_regional_report_period | is_active, status, issued_at, account_id |
| payments | ix_payments_regional_report_period | is_active, status, paid_at, account_id |

Both are ordinary, live, non-unique B-tree indexes with ascending keys, nulls
last, no expressions, predicates, included columns or constraint dependencies.
Valid exact indexes are preserved without DDL. Unexpected definitions, objects,
table/schema ownership or an active build fail closed and require review.

## Failure and safe retry

Production deployment run 38002995253 completed its backup and migrations,
but its first concurrent invoice-index build timed out waiting for a lock.
PostgreSQL left `indisvalid=false`. The old `IF NOT EXISTS` retry skipped that
object, advancing Alembic history while leaving an unusable reporting index.
The deployment schema check correctly stopped before replacing services.

PostgreSQL describes the invalid-object behavior and concurrent rebuild
recovery in its [CREATE INDEX documentation](https://www.postgresql.org/docs/16/sql-createindex.html).
The helper now checks catalog structure before any DDL, drops only an exact
interrupted owned index **concurrently**, creates it **concurrently** without
`IF NOT EXISTS`, and requires `indisvalid` and `indisready` before returning.
An exception propagates; a subsequent migration attempt repeats inspection.
The deployment's existing bounded lock timeout and retries remain in force.
No session is killed and no timeout is disabled. Statement runtime inherits
the migration connection's existing budget; concurrent construction scans
each table twice and can wait for older transactions. Schedule large/cohort
rebuilds accordingly rather than changing to a write-blocking index build.

## Already-applied databases

Do not delete Alembic version rows or downgrade. Revision 662 chains after 661
and repairs missing/interrupted builds even where 658 has already been recorded
as complete. Its downgrade retains both indexes because 658 owns them.
Fresh installs and retries of 658 use the same contract. A valid payments index
does not get rebuilt when only invoices needs repair.

Inventory using a read-only session:

```sql
SELECT n.nspname, c.relname, i.indisvalid, i.indisready,
       pg_get_indexdef(c.oid)
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relname IN ('ix_invoices_regional_report_period',
                   'ix_payments_regional_report_period');
```

Deliver the repair through the normal PR/main/exact-CI/candidate/staging/
authorization/production sequence. Do not edit production source or bypass the
schema gate. A changed candidate requires its own acceptance and authorization;
the post-migration resume of the previous candidate cannot install this new code.
Respect the active-release merge freeze: explicitly abandon the failed release
or finish its approved recovery before integrating a replacement candidate.
Retain the completed backup and use forward repair, not a schema downgrade.

## Evidence

`tests/test_regional_report_index_contract.py` covers typed catalog decisions,
both migration adapters, interruption/retry and non-destructive refusals.
`tests/integration/test_regional_report_index_recovery.py` requires an Alembic-
migrated disposable PostgreSQL database, reproduces a real lock-timeout leftover,
and upgrades a real 661 predecessor to the new repair revision. The ordinary
integration owner also proves fresh-baseline-to-head indexes. SQLite/mock tests
are fast-unit evidence only, never deployed-schema acceptance.
