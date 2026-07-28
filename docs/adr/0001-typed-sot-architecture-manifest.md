# ADR 0001: Typed source-of-truth architecture manifest

Status: accepted

Date: 2026-07-19

Decision owner: Michael / Dotmac architecture

Affected systems and domains: Dotmac Sub; all registered domains and adapters

## Context

`app/services/sot_relationships.py` names domains, service modules, free-text
concerns, and dependency edges. That index catches some missing or dead owners,
but it cannot prove what role owns each concern, which facts are authoritative,
where the transaction lives, how events and errors behave, how projections are
repaired, or whether an authority migration has actually cut over.

The repository has substantial indexed legacy debt. Requiring complete
contracts for all entries in one mechanical rewrite would create unverified
claims rather than trustworthy architecture.

## Decision

The relationship registry is the canonical architecture manifest. A fully
contracted service declares, with typed values:

- one role and authoritative-input mapping for every exact owned concern;
- the canonical writer for writer roles;
- transaction, locking, idempotency, retry, and stable domain-error behavior;
- versioned event delivery and replay behavior for state writers;
- freshness, stale behavior, drift signal, deterministic rebuild, and repair
  ownership for projections;
- native or explicit old-owner/new-owner migration and cutover state;
- accountable stewardship plus checked-in design and test evidence.

Uncontracted entries are temporary indexed legacy debt. Their service names are
recorded in a shrink-only baseline. A new service cannot enter that baseline;
removing a name requires supplying and validating its complete contract.

Generated manifest sections in the relationship map are derived from the
typed registry and checked for exact parity.

## Invariants

- Every contracted concern exactly matches one string in `SOTService.owns`.
- Every concern names one role and only writer roles name a canonical writer.
- Canonical writers name themselves; adapters cannot be canonical writers.
- Every authoritative input names a registered owner, except typed external
  observations whose owner uses `external:<system>`.
- Stateful writers own a transaction, stable domain errors, and a versioned
  event contract.
- Projection writers and reconcilers name drift detection and repair.
- New and migrated owners are fully contracted; the legacy baseline only
  shrinks.

## Consequences

Architecture review gains a machine-checkable contract and migration ledger.
Declarations become more verbose because authority evidence is explicit.
The baseline permits incremental migration but does not approve existing
architecture debt.

Free-text descriptions remain useful context, but they cannot satisfy a typed
contract field or suppress a validation failure.

## Migration and cutover

- Old owner and paths: existing uncontracted `SOTService` entries and their
  callers.
- New owner and paths: the same verified owner, represented by a complete
  `ServiceContract`, or a newly selected owner with an explicit migration.
- Backfill/repair: domain-specific and mandatory where persisted authority or
  projections move.
- Shadow or verification phase: represented by `AuthorityMigrationState` and
  its verification evidence.
- Cutover gate and evidence: required for every non-native migration.
- Fallback retirement: required for every non-native migration.
- Schema contract step: remove the service from the legacy baseline only when
  its typed contract and referenced tests pass.

## Verification

- Generic contract validation tests malformed roles, inputs, transactions,
  errors, events, projections, and migrations.
- Architecture tests reject new legacy entries and require the baseline to
  shrink after a contract is added.
- Design/test paths and generated relationship-map content must exist and match.
- Domain behavior tests remain required; manifest validity alone does not prove
  correct implementation.

## Rollback or forward-fix

The schema and checks are additive. A faulty contract is forward-fixed against
the implementation and evidence. Re-adding a migrated service to the legacy
baseline is not an accepted rollback because it discards an enforced boundary.

## Review and retirement

- Review date: when the legacy manifest baseline reaches zero.
- Retirement condition: none; supersede this ADR if the canonical manifest
  representation changes.
- Supersedes or is superseded by: none.
