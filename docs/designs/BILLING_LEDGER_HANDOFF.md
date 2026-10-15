# Installation-wide Billing ledger handoff

Status: implementation design, not deployed authority. The existing package,
local subledger latch and migration-plane declarations do not establish this
handoff. No production role capabilities or complete legacy catalog closure have
been verified. The full cutover remains blocked until the acceptance below is
proved on the exact released assembly and then authorized for production.

## One operational owner

Sub must provide one typed maintenance-only `runtime.billing_ledger_handoff`
owner around the published Billing opening-window and seal APIs. Billing owns
monetary facts, opening admissions and the permanent seal. Sub owns the complete
legacy-writer fence, deployment evidence and immutable installation receipt.
The receipt records an operation; it is not another ledger or a mutable provider
selector. There is no cohort, reset, fallback or dual-write path.

Caller source migration can proceed incrementally. Runtime Billing monetary
writers activate together, only after this committed global handoff. Before
activation, effective online module DML must be denied: ordinary Billing service
commands do not themselves require an opening seal. Installing the package,
selecting a module plane or setting a process-local authority binding cannot
replace this database privilege gate.

## Bounded implementation surfaces

- `app/services/billing/ledger_handoff.py` and typed contracts: final locked
  verification, existing Billing seal calls, immutable receipt and admission query.
- `app/services/billing/opening_export.py`: share the same owner's population
  collectors with a new locked final-verification API. Preserve the public
  preview's mandatory READ ONLY transaction.
- One receipt model and Alembic revision: singleton append-only operational
  evidence, initial ordinary module DML denial and permanent legacy guards.
- Existing commercial prerequisite/bootstrap owner: provision and verify exact
  offline, opening and online role separation. Do not create a second bootstrap.
- One thin maintenance adapter: invoke the owner transaction; no clear, reset,
  cohort, repair-history or bypass options.
- Runtime startup and financial command boundaries: verify committed receipt,
  matching seal and live fence before money-producing execution. Explicitly
  include web, worker, beat, CLI and direct database privilege paths.
- Existing deployment floor owner: recognize the permanent handoff/fence and
  refuse restoration of legacy financial writers; subsequent recovery is forward.

## Role separation and final transaction

Accepted Billing source checks `current_user` for restricted opening operations.
A non-superuser offline executor may therefore retain one `session_user` while
using transaction-local roles, provided its separately provisioned SET rights
are proved. The opposite membership direction must not give the opening
principal or any online login a path to admin or table ownership.

The runtime login must be nonowner. Revoking ACLs cannot fence a runtime table
owner, which can change its own guards. Verify actual login identity, role OIDs,
inheritance and SET-role reachability; do not infer them from configuration names.
No generic privilege grant, test-only role repair or unchecked SECURITY DEFINER
helper may substitute for the real production role contract.

1. Stop and prove the complete incumbent writer population and obtain stable
   drain evidence through the existing deployment owner.
2. Provision the opening window using existing Billing authority, then import
   qualified opening accounts under the exact restricted principal. Imports may
   commit while maintenance remains in force; online module DML stays denied.
3. Begin the final owner transaction. Acquire the required table locks before
   observing the final population, including locks needed to install the fence.
4. Recompute complete populations, economic watermark, role/catalog closure and
   exact writer/image evidence. Install owner-controlled permanent legacy guards.
5. Assume the bound opening principal with the correct tenant context. Call
   Billing's existing `seal_opening_window` with the trusted Sub verifier. Its
   second qualification under the window lock must re-observe authoritative
   facts in this transaction, not trust caller-supplied proof objects.
6. Return to the verified owner role. Grant only the reviewed ordinary Billing
   privileges; leave opening custody unavailable. Append the matching singleton
   immutable receipt and commit once.
7. Start the fixed final assembly's runtime writers only after committed-state
   receipt, seal and fence verification.

A failure during steps 3–6 rolls back fence, grants, seal and receipt together.
Previously committed imports stay restricted and unsealed; maintenance continues.
Actual non-superuser role choreography and transaction atomicity require real
PostgreSQL acceptance. Source-compatible SET ROLE sequencing is not that proof.

## Effective fence and retained product writes

Enumerate exact legacy financial tables and retained-column contracts. No runtime
wildcard may decide the archived population. Inspect owners, table/column ACLs,
PUBLIC, inherited and assumable roles, partition/inheritance children, rules,
triggers and reachable SECURITY DEFINER functions. Refuse unknown closure.

Archive fences deny INSERT, UPDATE, DELETE and TRUNCATE at statement level and
remove every effective online mutation path, while preserving reviewed SELECT.
Review privileged helpers individually; legitimate outbox infrastructure must
not be broken by indiscriminate function revocation. Migration 691's temporary
`UPDATE(id)` grants on invoices and subscribers are real DML and need explicit
final disposition.

Do not blanket-freeze shared product state: billing accounts, subscribers,
subscriptions, topup intents, service periods/entitlements, provider observations,
proof review, configuration and ERP queues require explicit retained/new-owner
column contracts. Any unclassified monetary, WHT, projection, reconciliation or
database-side writer remains a cutover blocker. A source AST census alone cannot
prove dynamic SQL and actual deployed grants absent.

## Immutable receipt and complete watermark

Use Billing's existing opening-window epoch, not a second mutable epoch. The one
installation receipt binds every scope/window/seal/epoch, manifest and admission
population digests, complete locked legacy population and economic cutoff,
source/package/image identities, writer-inventory/drain evidence, database
identity, observed roles/OIDs, fence catalog digest and exact operation authority.
Multiple tenant scopes still seal and activate in one global transaction.

Use typed, domain-separated complete populations in the watermark. Counts or an
old read-only preview hash are insufficient. Enforce matching seals, singleton
insertion and refusal of UPDATE, DELETE and TRUNCATE. The committed handoff is a
permanent rollback floor; post-handoff recovery never reopens legacy money writes.

## Required hosted acceptance

- Real non-superuser offline login: unchanged session identity, effective role
  changes, membership direction, tenant isolation and inaccessible admin paths.
- Before handoff: online ORM/direct SQL Billing mutations fail; restricted
  qualified opening imports succeed.
- Owner, PUBLIC, inherited/assumable roles, column grants, cascades and privileged
  helper attempts cannot bypass either archive or preflip module fences.
- Archived INSERT/UPDATE/DELETE/TRUNCATE fail while retained product writes work.
- Concurrent opening admission and seal cannot escape the final frozen population.
- Inject failures between fence, seal, grant and receipt; no partial activation
  commits and no legacy restart occurs.
- Exact replay is stable; changed scope, epoch, image, inventory, manifest or
  watermark refuses.
- After a real normal Billing posting, legacy SQL/scripts and old-image admission
  remain refused, while reviewed forward deployment succeeds.

Execute end-to-end on a production-shaped copy, with timings, economic
reconciliation, manual blocked-account queue and the pre-handoff abort boundary.
Only that evidence plus exact package and production approvals can admit the
single ledger switch. No runtime implementation or acceptance is claimed here.
