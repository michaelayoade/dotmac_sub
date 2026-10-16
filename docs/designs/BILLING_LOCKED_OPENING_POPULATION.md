# Locked opening population observation

Owner: `financial.invoices.opening_export`.

`capture_locked_billing_opening_population` is a census-only query for a future
registered global handoff owner. Its typed query wraps the existing explicit
source declarations. Those declarations, and any returned DTO, are never proof
of transaction ownership, locks, authority, qualification or permission to seal.
There is no consumer or handoff writer in this slice.

The existing public preview still requires an actual PostgreSQL READ ONLY
REPEATABLE READ or SERIALIZABLE snapshot. It retains the same V1/V2/V3 population,
classification and fingerprint contract. The two paths share `_population`,
`_sources`, `_settlements`, `_cash_evidence` and `_documentary_observations` in the
same owner; classification stays outside the shared observation collectors.
Canonical invoice settlement calculations continue through their existing owner.
The final census excludes lane/authority resolution, eligibility, exceptions and
controls. Raw cash DTO qualification and blocker collections remain empty; an
empty collection means classification was not performed, never approval.

## Actual transaction and lock discipline

The caller must already have a clean active PostgreSQL root READ WRITE
READ COMMITTED transaction and must acquire its final required table lock
strengths before calling. This query neither acquires nor upgrades locks, changes
isolation, flushes, begins/completes a transaction, nor issues DML. Pending ORM
state and SQLAlchemy nested transactions are refused. Caller-controlled prior SQL
must not release locks, use savepoints or mutate census inputs between capture
and final verification. The future command owner enforces this whole-transaction
invariant and performs seal verification inside that same transaction.

For every required census relation, PostgreSQL must report a granted relation
lock held by the current backend in the current database, with mode SHARE ROW
EXCLUSIVE, EXCLUSIVE or ACCESS EXCLUSIVE. SHARE alone is insufficient: competing
owners must be serialized as well as ordinary writers excluded. Unqualified ORM
relation resolution must equal the expected `public` relation, refusing temporary
or search-path shadows. Only ordinary, non-inherited tables are supported;
partitioned tables and inheritance parents/children fail closed until a reviewed
recursive fence inventory exists. PostgreSQL-held locks demonstrate the actual
transaction, rather than accepting `Session.begin()` alone as database proof.

READ COMMITTED is intentional. Every capture statement obtains a fresh snapshot
after all locks are verified; commits that completed while the caller waited for
locks are visible. A pre-existing REPEATABLE READ/SERIALIZABLE snapshot could miss
those commits even after acquiring locks and is always refused. The snapshot ID
labels the post-lock observation; it does not promise one repeatable snapshot for
the transaction. Required retained locks stabilize the selected populations
across subsequent statements. The future owner must obtain its final DDL-strength
locks up front, retain them through final verification and commit, and refuse any
intervening mutation of the captured population.

## Bounded inventory

V1 requires `alembic_version` (capture metadata), `subscribers`, `invoices`,
`invoice_lines`, `payments`, `credit_notes`, `ledger_entries`,
`customer_posting_groups`, `customer_position_effects`, `payment_refunds`,
`payment_reversals`, `prepaid_funding_baselines`,
`customer_subledger_opening_positions`, `payment_allocations`,
`credit_note_applications`, and `prepaid_opening_funding_consumptions`.
V2/V3 additionally require `payment_settlements`, `payment_provider_events`,
`integration_inbox`, `payment_proofs`, `payment_proof_corrections`, and
`topup_intents`. V3 documentary fields come from the already fenced invoices and
lines. Canonical settlement joins are covered by the same inventory. Preview-only
authority/lane dependencies are intentionally absent from the final census.

This inventory is **not a complete legacy fence**, watermark, role/credential
closure, DDL policy or global handoff certificate. It contains only populations
already selected by this exporter. The future handoff owner must combine it with
the complete independently reviewed fence inventory; no authority switch,
qualification, import, seal, financial DML, dual write or history rewrite is
introduced here.

## Validation

Hosted canaries in `tests/integration/test_billing_opening_export.py` exercise
missing/weak locks, absent transactions, stale repeatable snapshots, required
read-only preview preservation, V3 population parity and no-write behavior.
The two-session race deliberately reads before locking, waits behind an active
invoice writer, then observes that writer's committed value after lock acquisition.
Architecture guards check shared ownership and actual PostgreSQL proof queries.
Only stdlib AST and pinned Ruff 0.15.1 static validation are authorized locally;
all behavioral and migrated PostgreSQL validation remains required in hosted CI.

The locked query additionally refuses nonempty `schema_translate_map` execution
options on both the bind and the effective connection: textual lock proofs do
not follow ORM schema translation. A closed typed model inventory supplies the
relation names. Before reading any population, public SQLAlchemy mapper and
Table bind resolution must both equal the default lock-check bind, and their
resolved connection and transaction must be the same objects. Alternate mapper
or Table bindings are refused before opening another transaction. Session
factories using inherited built-in routing remain supported; overridden routing
or connection resolution is unsupported and refused rather than sampled.
Hosted refusal canaries cover bind/connection translation and mapper/Table
routing for invoice, cash, and canonical settlement helper inputs before any
population read. Preview-only authority and position resolvers stay excluded.

Effective connection savepoints are also refused, including externally opened
`Connection.begin_nested()` scopes invisible to `Session.in_nested_transaction()`.
A hosted canary pairs this refusal with successful capture on an ordinary
external root connection; savepoint rollback cannot invalidate an accepted fence.
