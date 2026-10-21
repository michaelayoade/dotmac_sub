# ADR 0030: Preserve applied field migrations through explicit convergence

Status: accepted — explicitly approved by Michael on 2026-10-12

Date: 2026-10-12

Decision owner: Michael

## Context

Published base `3dd49c6bee487730dceebbf7e3930773d001dbf4` contains
`671_customer_connection_type_definitions`, parent `670_user_credential_version`.
Field lifecycle revisions 671 through 682 share that original parent and have
already been applied to isolated QA databases. The existing sequence owner
requires a linear extension and rejects the preserved fork even though
no-op bridge 683 joins both histories. Renumbering identities or rewriting
parents would invalidate recorded history. This is a contract conflict, not
permission to skip the gate.

## Decision

Narrowly extend ADR 0008 through the existing
`scripts/architecture/migration_sequence_gate.py` owner. Ordinary admission
remains linear. A typed preservation declaration must name this accepted ADR,
the exact published base, immutable source-artifact provenance, the complete
revision/parent inventory and SHA-256 hashes. It covers only the published
connection-type 671, field lifecycle 671–682, inert bridge
`683_field_lifecycle_connection_types_merge`, and additive
`684_customer_connection_type_defaults`. Missing acceptance evidence fails
closed. Approval was explicitly granted in this session; implementation still requires the verification gates below.

Preserve every revision ID, parent and migration body. Rename only the field
671 filename to `685_assignment_generation.py`, following the documented
prepaid-purchase convention; filenames do not determine execution order.
Bridge 683 must join exactly lifecycle 682 and published connection-type 671;
684 must be its sole declared successor. No branch-name exemption, CLI waiver,
collision-baseline increase or new admission owner is permitted.

## Invariants and consequences

The existing owner must reject duplicate IDs before dictionary construction,
missing parents, cycles, altered or removed base migrations, extra forks and
multiple host heads. External module heads retain ADR 0011 governance.
Inventory, hashes and exact base must match the declaration. Bridge functions
may contain only inert statements and literal metadata; executable top-level
side effects are forbidden. Parent tuple ordering does not affect admission.
Undeclared future changes cannot reuse this exception.

Static convergence cannot prove compatible database effects. Review reads,
writes, raw SQL, constraints and triggers on both branches. Any incompatible
unordered dependency blocks acceptance. This preserves QA accounts and data
without stamping, schema substitution or historical rewrite.

## Cutover and verification

Implement the explicitly approved decision with positive and adversarial owner
tests. Preserve current CI enforcement modes. Require ordinary linear success,
stale fork refusal, exact declared graph success, and refusal of missing or
altered declarations, hashes, parents, base, duplicate IDs, orphan parents,
cycles, extra forks, multiple heads or executable bridges.

Existing evidence: v16 rehearsal reaches 684 from fresh, exact published main
671, lifecycle 680, lifecycle 682 and affected bridge 683. All five have equal
52-table schemas, defaults, constraints, indexes and triggers. Evidence is
`/private/tmp/dotmac-final-v16-schema-evidence.json`, bound to manifest SHA-256
`50f8a5d06eefe9cf68e3ecd243a70cb62b029ef6ae17c8e6a899d517aee1548e`.
The renamed migration is byte-identical to v16, SHA-256
`00f3de074016f91b7e5eb63086f2fcc3de3e0d87ec87fd3204fac5d66b9fa465`.
Review and bind the final source inventory before acceptance; run exact-base
sequence admission and repository gates. Stale local-main success is invalid.

## Rollback and retirement

Before deployment, source changes are reversible without database action.
After a database records a revision, retain its identity and body and use
reviewed forward fixes. No version-table stamping, parent rewrite or destructive
downgrade is authorized. No deployment is authorized by this decision.

Review on 2026-10-19 and before publication/deployment. Once this exact history
is incorporated into the accepted base, future branches cannot reuse its
preservation admission. Retain the historical evidence. This accepted decision narrowly extends ADR 0008; it does not change ADR 0011.

## Database compatibility review

Independent source review on 2026-10-12 found no direct unordered dependency
conflict for these exact histories. Published connection-type 671 creates and
seeds `customer_connection_types`, then changes `subscribers.connection_type`
and its lookup constraint/index. Field 671–682 owns assignment generations,
identity receipts, work-order classifications and vendor attendance, engineering
approval/issuance, completion review/replacement/follow-up, support resolution,
outage scopes and explicit project/task completion. Its only shared subscriber
dependency is 682's FK to the pre-existing `subscribers.id`; it neither reads
nor writes the connection-type lookup or column. Internal field dependencies
remain ordered by unchanged parents. Bridge 683 is inert; 684 changes only
the two connection-type defaults.

This is a bounded review, not a general commutativity claim. Published 671's
`ON CONFLICT DO NOTHING` seed does not reconcile differing pre-existing
lookup values. Its destructive downgrade and field migrations that refuse
downgrade reinforce the forward-fix policy. The five-path schema evidence is
a rehearsal artifact; it does not prove compatibility with arbitrary live data.

The declaration was re-pinned to current main `667ca9b6d11213d8593153905677b4ba43e69708`
before publication. Comparing its host migration directory with original
reviewed base `e55ea88be18496baf2c1325277c46ef1272b752c` yields no changes.
This updates the exact commit boundary after unrelated security/mobile changes;
it does not admit additional migration histories or alter reviewed migration bytes.

The preceding declaration was pinned to main
`3dd49c6bee487730dceebbf7e3930773d001dbf4`. The authentication and prepaid-billing
changes since `667ca9b6d11213d8593153905677b4ba43e69708` contain no migration
changes. All preserved migration identities, parents, bodies and source-artifact
hashes remain unchanged; only the exact base and declaration digest advance.

## Amendment: published router-health convergence — 2026-10-12

Main advanced to `a2878c1f380cbedcaa13553ddff2dbdc383103bc`, adding
`672_router_health_reconciler` after published connection-type 671. The previous
approval did not cover this additional published history or its convergence.

Amendment status: accepted — explicitly approved by Michael on 2026-10-12.

Michael approved this concrete amendment in this session before implementation.

The approved change preserves all previously declared migration bytes, IDs and
parents and preserves published router-health 672 exactly. Add inert
`686_field_lifecycle_router_health_merge`, with exactly two parents:
`684_customer_connection_type_defaults` and `672_router_health_reconciler`.
Rename only the existing field-identity filename from
`672_field_identity_review.py` to `687_field_identity_review.py`; retain revision
`672_field_identity_review`, its parent and its byte-identical body. These
filenames avoid collisions and do not determine migration execution order.

Extend the same typed declaration and sequence owner to pin the new exact base,
published revision hash, new bridge hash, and renamed path with original
provenance. Keep both bridges inert and all graph, immutable-history and
undeclared-extension refusals. Add adversarial tests for altered/executable new
bridge, missing published parent, changed published body, stale base and extra
heads. No stamping, destructive downgrade or admission bypass is proposed.

Require a fresh migration rehearsal, upgrade from actual published main 672,
and upgrade from the already validated field 684 database, including schema
equality of the router-health evidence tables/index and preserved lifecycle
objects. Run canonical repository gates on the final source. This amendment
authorizes source convergence only; it does not authorize deployment.


### Non-migration main reconciliation

The exact pinned base advances to
`81c4b4ccd032959de6f455991c123a83bdccd674`. The five main commits since the
accepted router-health base add authentication, customer-deletion, Inbox and
coverage changes. The complete `alembic/` and `app/models/` trees are unchanged.
All preserved revision IDs, parents, bodies, bridge bytes and source-artifact
hashes remain unchanged; only the exact base and declaration digest advance.
This rebase adds no migration admission or deployment authority.


The subsequent non-migration reconciliation advances the exact base to
`b69f1b4a6d088522ce5834aa57ccd94a3530606d` (#3579). It retires location
writes from customer detail reads and updates associated display and test
contracts. The complete `alembic/` and `app/models/` Git trees are identical
to the preceding pinned base. Preserved inventory, revision IDs, parents,
bodies, bridges and source-artifact hashes remain unchanged; only the exact
base and its declaration digest advance. This adds no migration admission
or deployment authority.
