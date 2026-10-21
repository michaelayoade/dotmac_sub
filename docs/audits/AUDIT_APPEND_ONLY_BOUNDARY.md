# Audit application append-only boundary

`observability.audit_log` owns audit persistence and queries. Runtime application code
may append through the existing canonical stage/deferred surfaces and read
persisted evidence. `DELETE /api/v1/audit-events/{event_id}` and
`AuditEvents.delete` are retired: deletion now receives 405 and is absent from
OpenAPI. No replacement mutation command is introduced.

Historical `is_active` values retain their read meaning: the default list and
`is_active=true` return active rows, `is_active=false` returns inactive rows, and
GET by identifier still returns either. Existing inactive evidence is not
rewritten. Stage still adds a new row in the caller transaction and dual-writes
legacy metadata and kernel details; rollback removes the staged append.

`tests/architecture/test_audit_append_only_boundary.py` guards runtime `app/`
new mutation sites across ORM field changes, field/nested metadata deletion,
query bulk mutation, Core UPDATE/DELETE, mapping bulk updates and literal SQL
(including schema-qualified and quoted TRUNCATE targets). It follows local aliases,
query results and typed parameters. Dynamic reflection/imports, cross-function
provenance and generated SQL are disclosed static-analysis limits. This source
guard is not a database permission or trigger and does not establish database
immutability.

The runtime mutation inventory is empty and the guard permits no baseline
exceptions. The unused `web_subscriber_actions.toggle_comment_todo` helper and
its orphan audit imports are retired: repository consumer search found no
product caller or route. Historical subscriber timeline `is_todo` and
`is_completed` flags remain readable, covered by
`tests/test_subscriber_comment_todos.py`. No replacement state owner or command
is introduced for the unused helper.

Non-runtime mutation inventory (retained, outside the runtime guard):

- `alembic/versions/413_audit_actor_label.py`: historical actor-label UPDATE
  backfill from the snapshotted metadata. The historical migration is unchanged.
- `scripts/migration/kernel_lineage_rehearsal_canaries.py:215`: disposable
  rehearsal UPDATE restoring synthetic historical `created_at = NULL`; this is
  not an authorized production maintenance writer.

No trigger, grant, role, purge job, correction column or retention activation is
part of this slice. The next database enforcement/maintenance slice requires
S11's exact maintenance identity and Q-A6 CLI attribution decisions. The approved
365-day retention policy and holds remain unactivated here.
