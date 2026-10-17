# Kernel lineage entry prototype

Status: source prototype, entry-only; not adoption and not an operational handoff.
Owner: Sub platform operations, the existing minimized-rehearsal owner.
Implementation: `scripts/migration/kernel_lineage_entry.py`.
Caller: `tests/integration/test_kernel_lineage_entry.py`, hosted disposable fixtures only.

The participant deliberately has no command-line entrypoint, runtime caller,
engine, connection factory, transaction completion or ordinary deployment
composition. A fixture database name is a defensive boundary, never operational
approval. The caller supplies an already active PostgreSQL root transaction,
without a savepoint, and must rollback even after successful entry.

## Bound source and artifact

The source checkpoint is Sub
`e119f08f720779fcb9c46797f568f140bad88168`; its own current head is
`690_backdated_invoice_permissions`, alongside genuinely composed module heads.
Its unchanged pyproject and lock file pin Kernel `0.1.0a105`.
The locked wheel digest is
`f8937e5525c3efd27e2030bf69ed0c37e137c345e376668a79e00ebf349b6ffa`.

Kernel's annotated a105 tag `bd5f252b936efb3ee61aeb871b915f2e41bab754`
peels to `2f5abe9101c9aef48f23977cad81a3e499672287`. The released versions
directory contains 28 revision files and ends at `0028_machine_attribution`.
All 28 tag Git blob identities are embedded in the participant. Hosted execution
first checks installed distribution metadata, the exact file set and each
installed file's Git blob identity. It then loads the installed revisions through
Alembic. There is no copied migration implementation, stamp, existence-skip,
DDL interception, lower dependency floor or proposed audit revision 0029.

The GitHub tag listing and source-byte comparison are static source evidence.
They do not prove the local a97 environment is the pinned installation, verify
registry wheel bytes, or establish actual migrated-schema acceptance. The hosted
lockfile install and complete-chain tests remain required.

## Fixed closure and independent observations

The fixed fifteen relations are audit_events, communication_suppressions,
domain_setting_history, domain_settings, idempotency_records,
machine_credentials, outbox_events, parties, party_roles,
platform_idempotency_records, platform_outbox_events, roles, tenant_domains,
tenants and user_credentials. This includes Kernel 0003's transient party_roles
collision, even though 0022 renames its grant relation. Inventory of the verified
installed chain is intersected with the actual fixture catalog before any move;
a difference refuses entry.

Four existing relay signatures move, not just the two composite-return claims:
claim_outbox_batch(text,integer,integer),
settle_outbox_event(uuid,text,text,timestamptz,integer,text), and both platform
equivalents. Both settle writers also hard-code public targets and run with
SECURITY DEFINER. Their original OIDs, return types, full definitions, owner,
security settings and ACLs are observed. Original definitions are not rewritten.
Explicit function grants are revoked, and the fresh escrow schema denies USAGE
to ordinary non-owner roles, fencing retained column privileges and implicit
function-owner EXECUTE. Superuser and privileged-owner containment is not proved.

The catalog plan is independently observed, rather than enumerated from the move
list. It covers non-system relations and columns, effective grant inputs,
constraints and incoming FK OIDs, index definitions, dependency edges, policies,
rules/views, trigger and function definitions, defaults, role posture and role
membership. Incoming views/rules and unaccounted catalog-known callable paths
are refused even before a new plan is observed. A recursive pg_depend closure
starts from the collision relation/composite-type OIDs and four approved relay
function OIDs; it follows function/type dependencies, including parsed SQL
scalar-return bodies and transitive function wrappers. Only the four reviewed
relay functions are admitted in that callable closure. Additional consumers
need explicit dispositions. Catalog drift, even a column grant,
refuses execution. Complete SQL-caller discovery, string-built dynamic SQL and
effective privileged-helper closure remain operational gates; catalog stability
is not evidence of runtime writer retirement.

Before movement, all actual non-system fixture tables are locked deterministically
with bounded acquisition waits, including incoming-FK relations and genuine
version histories. The catalog is observed again under those locks. This broad
lock set is deliberately a disposable-fixture probe, not a proposed production
lock budget or proof that writers have been drained.

ALTER TABLE SET SCHEMA preserves original relation and composite-type OIDs and
their dependent objects. Incoming Sub FKs continue to point to escrow; they are
not silently retargeted by the new public spelling. Original Sub histories never
move. The complete installed Kernel chain runs on the same supplied Connection,
with its genuine independent dotmac_kernel_alembic_version table and ordinary
Alembic Operations. Kernel-generated canonical destinations must have new OIDs.

## What the hosted tests qualify

The tests use a clone produced by the complete real Sub/composed-module Alembic
chain. They populate all fifteen collisions, including local, RADIUS and SSO
credential contexts, unknown audit persistence times, quarantined product Party
state, active/inactive roles, temporal business capacities, consent, settings,
machine credentials, idempotency and both outbox planes. Existing canaries create
synthetic structural data; no external credential is converted to a password,
and no issuer, Party correspondence or historical adoption ceremony is inferred.

Successful entry must leave the caller's root active, preserve every populated
escrow row, preserve incoming FK and both old composite return OIDs, create new
canonical OIDs and relay functions, reach the actual installed Kernel head and
leave Sub heads untouched. Caller rollback must restore original rows, catalog
definitions, grants, OIDs and histories. Explicit faults after locking,
relocation and complete Kernel execution exercise the same rollback contract.
Additional cases refuse catalog drift, unknown dependent callable/view paths
(including real SQL-standard BEGIN ATOMIC scalar readers, relay wrappers and
transitive wrappers created before plan observation),
replay, missing root transaction, nested transaction and wrong artifact plans.

These tests are authored but unrun locally. No SQLite/create_all result is
migration, constraint or transactional evidence. The existing direct-overlay
EXPECTED_FIRST_FAILURE at 0001 remains unchanged and still represents an open
Sub reference gate.

## Required work beyond entry

This prototype has no canonical Party import, credential/provider import,
product extension DDL, FK transfer or runtime writer cutover. Kernel's empty
canonical identity rows cannot replace Sub's populated authority. Product Party
status/classification/merge evidence, Role description/activity, credential
binding/version/lockout/MFA context, nullable audit history and business
party_roles still need explicit lossless destinations and existing-owner
participants. The existing flush-only stage_credential_party_binding participant
is available for reviewed reuse; its committing command must never be nested.

A genuine separate Sub-owned post-Kernel adoption lineage must construct
extensions only after unchanged-chain execution and canonical Party import,
asserting actual canonical target OIDs and required exact FK constraints.
Today's ordinary Sub heads path must not create FKs against predecessor OIDs.
Future ordinary composition must truthfully advance the Sub, Kernel and adoption
histories and prove a later additive Kernel revision through that same owner.

Durable maintenance closure, complete live source/provider dispositions,
writer denial/drain, effective privileged access sealing, plan approval and
custody, attributable transactional receipt, lost commit acknowledgment
recovery, independent post-commit actual-login acceptance and successor process
admission remain required. These gates cannot be replaced by a matching fixture
bit, an empty destination or a green entry test. After successor writes begin,
frozen escrow is stale; reverse recovery needs a proved transfer of new writes
or an approved forward repair.

The current atomic OIDC ceremony/session contract and principal → credential →
Party → binding → session order are unchanged. No standing epoch, authentication
activation, feature flag, provider registry or Kernel API is added.

