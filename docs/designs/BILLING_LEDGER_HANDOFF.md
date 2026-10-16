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

## Overlap before the single switch

Steps 1–3 advance concurrently. Package publication is not a prerequisite for
source caller migration or opening-data preparation. Consumer runtime acceptance
does require the exact published package set; source candidates must not be
presented as installed or released packages.

| Step | Work that proceeds now | Evidence needed to finish |
| --- | --- | --- |
| 1. Starter package set | Resolve candidate CI failures, review source and publish approved versions through the provenance workflow. | Exact published versions, artifact digests and truthful release records. |
| 2. Sub callers | Migrate payment, prepaid, invoice, credit/WHT, rendering and ERP-fact callers against public contracts; remove their legacy write paths for the switch. | Hosted consumer acceptance on the exact published pins and a complete writer inventory. |
| 3. Opening balances | Export the incumbent population, map accounts and independently qualified original documents and cash evidence, and rehearse the existing importer. | Per-account reconciliation and a manual queue for every blocked or unexplained item. |

Rehearsals feed failures back to all three steps while work continues. Synthetic
fixtures prove importer behavior; authentic opening evidence proves the proposed
production balances. Once the three steps converge, execute the complete timed
rehearsal, then the authorized freeze, drain, final import, permanent seal and
single installation-wide handoff. Verification and retirement follow the switch.

## Bounded implementation surfaces

- `app/services/billing/ledger_handoff.py` and typed contracts: final locked
  verification, existing Billing seal calls, immutable receipt and admission query.
- `app/services/billing/opening_export.py`: share the same owner's population
  collectors with a new locked final-verification API. Preserve the public
  preview's mandatory READ ONLY transaction.
- One receipt model and Alembic revision: singleton append-only operational
  evidence and initial ordinary module DML denial. Permanent legacy denial
  becomes effective only inside the final handoff transaction.
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

The bounded `runtime.billing_runtime_login` diagnostic is implemented by
`require_billing_runtime_login` in `app/commercial_module_prereqs.py`. Its frozen
query inspects the supplied actual psycopg connection on PostgreSQL 16 or later,
requiring an active READ COMMITTED transaction. libpq's startup user and both
PostgreSQL identities must be `app_user`; administrative `SET ROLE` and
`SET SESSION AUTHORIZATION` do not qualify. It rejects elevated role flags,
every distinct effective MEMBER/USAGE/SET path (including unknown or dormant
memberships), and any relation/schema/function/database ownership in the current
database, including catalog and temporary namespaces without exemptions.
This conservative diagnostic does not grant an exception for an apparently
harmless role. It reads no password catalog and never opens a connection,
completes a transaction or changes roles. It is callable explicitly; default
bootstrap and runtime startup behavior are unchanged.

Returned database/backend/role OIDs and transaction identity are observations,
not reusable admission evidence. The caller owns the root transaction: libpq's
INTRANS status cannot prove that no manually issued SAVEPOINT exists. Exact
Billinga1 migration-derived catalog shape, PUBLIC and column grants, privileged
helper/outbox closure, preflip revocation and locked final admission remain
unimplemented by this boundary. Its authored hosted PostgreSQL canaries have
not yet been executed. Their role mutations require the existing isolated
`integration-shards` PostgreSQL service, prove pristine posture/memberships and
an originally NULL password using boolean checks, and hold a shared
maintenance-database advisory lock through restoration. The lock coordinates
participating fixtures only; the hosted service and serial suite supply cluster
isolation. Unknown prior state refuses before mutation. This does not establish
complete preflip denial, an opening seal, a legacy fence or ledger activation
readiness.

No generic privilege grant, test-only role repair or unchecked SECURITY DEFINER
helper may substitute for the real production role contract. Provision the exact
selected-plane opening/idempotency/outbox closure required by Billing
`bi_0009_opening_window`, plus reviewed SELECT for the final verifier. The opening
principal receives no ordinary/admin membership. Reverify effective preflip
module DML denial after subsequent module migrations, which may introduce grants.

Provisioning requires effective `app_admin`; import and seal require the bound
opening principal; legacy DDL requires the verified actual table owner. The
offline executor remains the session identity throughout.

1. Stop and prove the complete incumbent writer population and obtain stable
   drain evidence through the existing deployment owner.
2. Provision the opening window using existing Billing authority, then import
   qualified opening accounts under the exact restricted principal. Imports may
   commit while maintenance remains in force; online module DML stays denied.
3. Begin the final owner transaction. Acquire the required table locks before
   observing the final population, including locks needed to install the fence.
   Use a fresh snapshot that observes every legacy commit preceding successful
   lock acquisition; a pre-existing REPEATABLE READ snapshot is insufficient.
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
wildcard may decide the archived population. The source-derived starting inventory
is recorded in `BILLING_LEGACY_FENCE_INVENTORY.md`; it is not catalog closure. Inspect owners, table/column ACLs,
PUBLIC, inherited and assumable roles, partition/inheritance children, rules,
triggers and reachable SECURITY DEFINER functions. Refuse unknown closure.

Archive fences deny INSERT, UPDATE, DELETE and TRUNCATE at statement level and
remove every effective online mutation path, while preserving reviewed SELECT.
Review privileged helpers individually; legitimate outbox infrastructure must
not be broken by indiscriminate function revocation. Migration 691's existing
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
- Reject online owner/admin/assumable-owner identities. Prove PUBLIC, inherited
  privileges, column grants, cascades and reachable privileged helpers cannot
  bypass archive or preflip module fences. Offline owner/admin authority remains
  trusted; an owner can alter its own guards.
- Archived INSERT/UPDATE/DELETE/TRUNCATE fail while retained product writes work.
- Concurrent opening admission and seal cannot escape the final frozen population.
  Race a legacy commit before lock acquisition and prove final capture sees it.
- Inject failures between fence, seal, grant and receipt; no partial activation
  commits and no legacy restart occurs.
- Exact handoff replay is stable; changed scope, epoch, image, inventory, manifest
  or watermark refuses replay of that operation. Reviewed forward images consume
  the existing receipt through deployment compatibility checks; they do not
  reseal or require equality with the original handoff image.
- After a real normal Billing posting, legacy SQL/scripts and old-image admission
  remain refused, while reviewed forward deployment succeeds.

Execute end-to-end on a production-shaped copy, with timings, economic
reconciliation, manual blocked-account queue and the pre-handoff abort boundary.
Only that evidence plus exact package and production approvals can admit the
single ledger switch. No global handoff implementation or activation acceptance is claimed here.

## Private archive participant status

`runtime.billing_legacy_archive_fence` now declares a private flush-only
participant for the inventory's exact 44 archived tables. Its only entry point
is `_install_legacy_archive_fence`; no runtime, command, migration or startup
caller exists. It refuses invocation unless the future
`runtime.billing_ledger_handoff` is actually registered with a valid coordinator
contract and is the active root owner. That global owner remains unimplemented
and unregistered by this slice. See the inventory's private component contract
for role, locks, trigger/ACL and exact replay conditions.

A new installation stages one typed component audit event in the same owner
transaction; it does not write the global receipt, seal a window, grant module
DML or admit runtime writers. Every failure must escape and roll back the entire
owner transaction. The whole owner must forbid manual SQL savepoints throughout
its transaction. Returned transaction/OID/digest observations are facts about
uncommitted staging, never reusable authorization. Helper/view/FK/privileged
closure, retained columns, drain, preflip denial and the installation-wide
handoff remain unresolved. Real migrated hosted component canaries are authored;
no database/runtime execution or production acceptance is claimed.

The participant uses public `current_owner_root_transaction` introspection to
match the executor's exact active root. This follows the executor's trusted
in-process metadata contract; it is not a security boundary against arbitrary
Python code rewriting executor internals. Connection and audit-model bind checks
additionally refuse external roots, nesting and split transactional delivery.

## Private retained-column component v1

The existing `runtime.billing_legacy_archive_fence` owner also contains the
private `_install_legacy_retained_columns_fence` participant. Its closed policy
is exactly `billing_accounts.balance` and `subscribers.deposit` (numeric(12,2),
existing UUID primary keys). It reuses that owner's root, role and bounded-wait
proof, requires the same physical audit/owner connection and locks both ordinary,
unpartitioned, noninherited tables before mutation and reobservation. The final
handoff coordinator must prelock the sorted global union before final observation;
component-local locks alone do not establish final population capture.

Account insertion requires exactly zero balance; subscriber insertion requires
NULL deposit. UPDATE preserves the exact NULL-sensitive value and primary key.
DELETE permits only zero account balance or NULL subscriber deposit: a historic
subscriber deposit of zero is recorded history and cannot disappear. Existing
FK/lifecycle checks remain. This deliberately narrows the existing hard-delete
owner's deposit-row behavior; soft lifecycle transitions remain available.
AFTER INSERT/UPDATE row guards check the final tuple even when another BEFORE
trigger changes it. ALWAYS DELETE guards and statement TRUNCATE guards also apply
in replica mode. Functions are fixed SECURITY INVOKER with pg_catalog search path,
direct OLD/NEW comparisons and no joins or RLS-dependent lookups. The component
revokes only nonowner/PUBLIC TRUNCATE, including grant options, using RESTRICT;
shared-table DML and column grants remain usable and row guards enforce policy.
Exact catalog replay emits no duplicate transactional
`billing.legacy_retained_columns.staged` audit; partial installation, body, timing,
arguments, ownership, enabled-state or TRUNCATE ACL drift refuses without repair.

`min_balance` remains commercial policy, `prepaid_low_balance_at` notification
state and `mrr_total` a writable cache. Its current unregistered MRR writer is
RETIRE_AFTER_CUTOVER; a registered Billing-fed balance display owner and explicit
MRR disposition remain final handoff gates. This component does not bless these
legacy writers permanently, classify privileged helper closure, seal, write the
installation receipt, activate, commit, grant module DML or unfreeze writers.
There is no production caller, migration, CLI or startup wiring. Hosted migrated
PostgreSQL canaries are authored; no local database/runtime execution or production
acceptance is claimed.


## Narrow relay attachment closure (source candidate)

The same private fence owner contains `legacy_privilege_closure.py`. Its closed
archive lock population is 44 archives plus `public.outbox_events` and
`public.platform_outbox_events` (46); its retained lock population is both retained
tables plus those relay tables (4). These components have a 48-table union;
the explicit V3 census additionally requires `alembic_version`, `integration_inbox`,
`payment_provider_events`, `payment_proofs`, `payment_proof_corrections` and
`topup_intents`. Locked preparation derives the complete union from the public
typed census inventory and existing component policies, currently 54 relations.
One sorted ACCESS EXCLUSIVE lock statement precedes capture and all ACL/DDL/audit
mutation. The future coordinator is still absent and no activation is installed.

The private `_stage_locked_legacy_handoff` belongs to the existing
`runtime.billing_legacy_archive_fence` participant owner. It requires the actual
active final-owner root, stages both real fence components, and compares fresh
same-root V3 populations before and after their effects. Observation clocks and
snapshot labels are excluded from semantic equality. Its frozen result records
actual locks, census and staged component observations. The caller retains the
whole transaction: failure rolls back guards, ACLs and audit outputs together.
This participant has no standalone committing adapter or authority receipt and
does not establish the complete economic watermark, helper closure, opening
qualification, deployment identity or ledger admission. Hosted acceptance is
pending; test-only final-owner registration proves component behavior only.

The closure verifies the four exact claim/settle helper bodies from Sub migration
557, their signatures, return/language/config/owner and exact EXECUTE ACLs. Helpers
remain `app_admin` SECURITY DEFINER with empty search path. The definer must
retain BYPASSRLS and effective SELECT and UPDATE on both relay tables; missing
capability refuses without grants or repair. Validated return type OIDs and set
semantics enter the digest, so TEMP shadowing cannot alter it through type-name
deparsing. Relay tables must belong to the actual ordinary component/public-schema
owner. It removes nonowner
and PUBLIC table TRIGGER/REFERENCES and column REFERENCES, including grant options,
with RESTRICT. SELECT, retained and relay DML and existing foreign keys are not
revoked. Actual online roles are checked for unsafe attributes, ownership, direct
and effective MEMBER/USAGE/SET paths, and CREATE on the `public` schema and
current database. PUBLIC checks inspect those explicit/default ACL entries
directly; no permanent probe role is created.
TEMP alone is not a refusal because these helper relation references are qualified
and attachment privileges are denied. No deployed-login claim is made.

Post-closure privilege/helper observations enter the component digest. Replay
compares the actual catalog with the original transactional component audit and
refuses drift without repair or another audit event. This proves only the exact
relay attachment path; broad helper/view/FK closure, watermarks, receipts and global
handoff remain unresolved. Protected legacy FK/trigger inventory and grant-chain
RESTRICT semantics still require review before this candidate is accepted. Hosted
migration-chain canaries remain required; no local runtime validation was run.

PostgreSQL 16 defines [TRIGGER and REFERENCES privileges](https://www.postgresql.org/docs/16/ddl-priv.html)
and the [CREATE TRIGGER requirements](https://www.postgresql.org/docs/16/sql-createtrigger.html).
## Invoice cash source foundation (source candidate, 2026-10-15)

`financial.provider_invoice_module_cash_source` is a read-only preparatory query
in the existing `provider_payment_settlements` module. Its typed input addresses
only a persisted verified observation and the original checkout reference. It
reads the Payments a2 public frozen checkout and exact original local link,
and refuses mismatched provider, payer, invoice, money, scale or original
lifetime. It returns source facts only; no Billing account ID crosses this
query as validated financial identity. It does not read current invoice
eligibility or substitute today's TTL.
It returns observations, not a caller-constructed permission to move money.

The source query and incumbent `financial.provider_payment_settlements`
orchestration share an implementation module but own distinct concerns. The
architecture guard pins exactly these two owners; it admits no third owner.
Legacy cash, allocation and outbox authority stays with the incumbent while the
query remains read-only. Remove this exact module split when all callers have
migrated to the sole Starter ledger and the incumbent orchestration is retired.

Revision `692_module_invoice_cash` expands the schema after 691. Its Billing
dependency is the already released `bi_0001_billing`: that migration declares both referenced composite
account and settlement/account keys. It does not depend on or claim release of
Billing a8. Binding and correlation tables use tenant RLS, cross-module composite
foreign keys, append-only update/delete/truncate guards and app_user SELECT-only
grants. The actual runtime login may own tables or reach an owner role; these
grants do not establish offline-only authority. Trusted binding provisioning,
actual owner/login separation and final catalog qualification remain missing.
No public binding CRUD writer is introduced and no historical row is backfilled.
The shortened revision fits Alembic's existing version column. Prospective tables
already emitted by the squashed base are accepted only after locked catalog
validation of the known empty column/default/constraint/index shape and each
local foreign key's exact four ordinary enabled internal RI triggers (including
referenced-side actions by constraint OID), with no
unknown rules, triggers, RLS policies, inheritance or population. The nullable
occurrence column must likewise match its declared default-free shape with no
prior occurrence facts or dependent constraints/indexes. Unexpected partial
installs refuse; IF NOT EXISTS does not silently adopt them. These catalog checks
are authored migration behavior, not hosted deployed-schema acceptance.

Provider observations gain nullable `occurred_at`. A future migrated verified
adapter must supply an explicit timezone-aware provider capture timestamp, such
as the already-parsed signed webhook `provider_paid_at`; transaction creation,
receipt, retry time and wall-clock defaults cannot substitute. The normalized
digest includes that fact only when supplied, preserving existing digest bytes
for current callers. Historical evidence cannot be replay-enriched with a new
capture time. First `received_at` remains the distinct durable observation time.
Current runtime dispatch and adapters are unchanged; observations without a
capture time refuse the preparatory query. Late receipt is valid when actual
capture falls inside the original checkout lifetime. A later confirmation must
explicitly bind the Payments timestamp semantics without rewriting either time.

The read-only query retains exact numerical, currency, identity and lifetime
bounds while returning frozen requested customer credit, provider gross, provider
fee and provider net as independent observations. It does not require provider
net to equal requested credit, or gross = net + fee. Neither equality has an
approved policy contract here. The future financial caller must establish a
reviewed immutable amount binding before confirmation or Billing credit; this
query grants no cash admission and chooses no fee bearer or credit calculation.

The positive flush-only module cash participant remains unimplemented because
Billing currently exposes no public frozen account observation to validate the
structural mapping against account external identity, currency and scale. Billing
accounts are mutable; a composite foreign key proves existence and scope, not
those facts. An upstream frozen account query is a required published contract;
Sub adds no cross-module mutation guard or assumed immutable external reference.
Also, no existing trusted resolver independently qualifies provider plus physical
merchant namespace. `source_system` must share that qualified namespace with
opening imports; `source_settlement_key` must be the exact provider transaction
ID (`event.external_id` / provider `data.id`), and source_version still needs its
canonical contract. Checkout reference, installation/configuration identity,
asserted PSP namespace and Payments confirmation UUID cannot replace these facts.
The correlation schema preserves the intended original observation/intent/
confirmation/account/settlement join, but inserts do not certify namespace or
handoff authority. Correlation rows remain append-only. Successor revision
`693_provider_source_facts` supplies the RLS-independent provider source-facts
integrity prerequisite. Its SECURITY INVOKER trigger, with pg_catalog search_path,
compares only OLD and NEW; it performs no reads or correlation/tenant joins.
The row guard runs AFTER UPDATE OR DELETE so it validates the final stored tuple
following every BEFORE row trigger, including a later alphabetically ordered
trigger that rewrites protected facts during an otherwise allowed lifecycle
update. TRUNCATE retains its BEFORE STATEMENT guard. Rejection rolls back the
statement and enclosing transaction; this is a source-integrity guarantee, not
proof of installation-wide helper or privilege closure.
Once the old source is `verified_webhook` or `gateway_reconciliation`, it freezes
id, provider_id, event_type, external_id, idempotency_key, source,
observation_digest, observed_payment_status, amount, provider_fee, net_amount,
provider_reference, currency, financial_effect, occurred_at and received_at.
Both guards are ENABLE ALWAYS; downgrade refuses guard removal and recovery
requires a reviewed forward migration. Equal values and equivalent timestamp instants
are permitted. All observation DELETE and TRUNCATE operations are refused,
including empty-table truncation and cascaded truncation. This guard does not
restrict INSERT or qualify any source enum as cash evidence; privileged source
assertion, normalized evidence and namespace qualification remain separate.

The incumbent owner still checks exact evidence before first legacy_unknown
provenance enrichment. There is no historical rewrite, backfill or repair.
After first verified admission, pending verified-to-verified replay preserves
that first source and normalized facts; it may replace raw transport payload
and change payment_id, invoice_id, status, processed_at, error_code and error.
The existing financial-observation digest deliberately excludes raw payload.
No SECURITY DEFINER bypass or cross-module privilege grant is introduced.
No positive cash writer or live correlation admission is mounted here.

After that dependency exists, the same existing root transaction must serialize
intent and account, call Payments `record_confirmation`, read the immutable
public confirmation, derive the narrow evidence allowlist internally, apply the
reviewed gross/fee/net binding, accept Billing `AVAILABLE_CREDIT`, and append
correlation/outbox with only flushes. Return typed allocation-pending evidence;
allocation is a later module caller migration. Do not create legacy financial
rows, dual writes or fallback after module acceptance. Full single authority also
requires removal of every incumbent caller, the global handoff and permanent
legacy fence; this query does not activate any writer or manufacture a receipt.

Hosted tests in `test_provider_invoice_module_cash_pg.py` are authored foundation
canaries only and have not run. They include real predecessor upgrade refusal
for malformed prospective schema or disabled FK enforcement, both native
CREATE/ADD and known-squashed-shape adoption, and typed informational observation-owner
canaries for aware-time persistence, exact/changed replay, NULL history, naive
time refusal and first receipt preservation. Informational events create no cash
confirmation or settlement and do not prove verified-money admission. They require real Alembic heads and an actual
non-superuser app_user login, with no test-only privilege repair. They reuse the existing isolated hosted
`runtime_connections` fixture, which owns its temporary test authentication,
maintenance-database lock, pristine-role checks and independent original-NULL
cleanup. No new credential environment or administrative SET ROLE is introduced. Required next
acceptance includes predecessor upgrade plus fresh baseline, tenant mismatch and
binding constraints, changed-fact replay, concurrent duplicates, rollback at
confirmation/posting/correlation, late webhook against opening cash claims and
original capture/receipt semantics. These financial tests await the qualified
namespace/amount contracts and published exact Billing a8 pin; current Billing
a1 cannot execute the new API. No path-installed package, create_all, fake final
handoff receipt or source-compatible candidate call is acceptance evidence.

The 692 absence/adoption canaries use a clone produced by the real current
migration chain. Because its squashed base declares today's prospective ORM
shape, the absence variant removes only the two empty future tables and nullable
occurrence column before applying 692. It asserts the resulting FK enforcement,
RLS and append-only trigger catalog. This is branch coverage, not proof of a
historical deployed image. The actual predecessor-image upgrade rehearsal and
production-shaped end-to-end acceptance remain required.

The 693 source-freeze canaries are authored, not executed locally. Hosted CI must
run the full real migration baseline and the real 692-to-693 predecessor rehearsal,
then the actual unprivileged app_user-login canaries in
`tests/integration/test_provider_invoice_module_cash_pg.py`. They cover unchanged
historical rows and preexisting triggers, ALWAYS/invoker/search_path catalog
shape, all frozen fields, later BEFORE-trigger fact mutation, hidden tenant binding context, equal values/equivalent
instants, processing/payload updates, both verified replay directions, strict
legacy enrichment, deletion, empty/cascaded truncation, atomic rollback and
independent requested/gross/fee/net reads with zero cash postings. Truncation is
exercised by the setup owner; the actual login receives no extra cascade-table
privileges. Full repository CI and qualified namespace/policy/account/authority
acceptance remain required before a later positive cash participant.
