# ADR 0012: Sub is the measured source for ISP replacement cohort 1

Status: accepted

Date: 2026-08-21

Decision owner: Michael Ayoade

Affected systems and domains: party_identity, customer_context, the
cohort-isp-01 persistence surface, and the future `asm-dotmac-isp` assembly

## Context

Governance accepted `pgm-dotmac-isp-replacement` at commit
`68c7a62e2aafd9c236662a5a69d410ea002b4cdb`. That record makes Sub
`asm-dotmac-sub-legacy`, the **source-authoritative** assembly, and assigns it
`track-isp-sub-cutover`: complete source dispositions, idempotent replay,
bounded shadow comparison, sealed cohort switches and displaced-writer
retirement. Cohort 1 is `cohort-isp-01`, "Foundation party and customer",
whose components are kernel and UI (reuse), `dotmac-party` (release),
`dotmac-brand-profiles` (adopt) and `dotmac-customers` (build).

Every cohort in that matrix is `blocked`. Two of the open decisions that block
it — `dec-isp-002` (production deployment owner) and `dec-isp-003` (the
enforceable legacy Sub transition rule) — are Michael's and are not Sub's to
resolve. So the question this ADR answers is not "when do we cut over"; it is
"what has to be *true and measured about Sub* before a cutover can even be
proposed".

Two things were not true before this decision.

First, nobody could state the cohort's writer surface. Cohort-1 rows are
written from twenty-six production paths across services, web presenters and
one-off scripts, plus eight more — fixture tooling and applied migrations —
that cannot write production again. Six of the twenty-six are declared owners;
the rest are debt. `subscribers.metadata` alone is written by seven different
modules, none of which declares its shape. A migration cannot displace writers
it has not enumerated, and a count nobody can reproduce is not an enumeration.

Second, there was no way to hand cohort facts to a destination without handing
it a database. Sub's reporting seam (`read_only_snapshot_session`) gives a
consistent read; it does not give a versioned contract, a stable opaque
identity, deterministic ordering, or a comparison artifact a shadow importer
can use without Sub credentials.

## Decision

Sub adds a product-owned, read-only **source-readiness** capability under
`app/migration_source/`, and freezes its cohort-1 writer surface with a
two-directional ratchet.

- **Authority is unchanged.** Sub remains the sole production decision and
  write owner for every cohort-1 fact. Nothing here composes a Starter module,
  adds a module migration, writes to a target database, dual-writes, disables
  a Sub writer, or moves a control state.
- `app/migration_source/programme.py` binds Sub to the accepted Governance
  revision. It is a read-only view; `CohortState` has no `open` member and
  `SourceReadinessClaim` has no member spelling adoption or cutover, so
  neither claim is expressible from the source repository.
- `app/migration_source/cohort.py` is the single declaration of the twelve
  Sub tables holding cohort-1 source state. The ratchet, the exporter and the
  digest all read it, so they cannot disagree about scope.
- `app/migration_source/surfaces.py` classifies every counted writer, every
  adapter that reaches the cohort, the cohort-adjacent tables deliberately
  left out, and the two cohort tables with no counted writer.
- `scripts/architecture/isp_cohort_writers.py` counts writes across declared
  entry-point families and proves no Python-bearing repository root escapes
  the census unexplained.
- The export snapshot and comparison digest are described in
  `docs/ISP_COHORT1_SOURCE_OWNERSHIP.md` and land in the same package.

## Invariants

- The Governance binding pins a 40-character commit. A tag or branch is
  refused at construction.
- One entity type maps to exactly one table; a duplicate is refused.
- A surface classified `AUTHORITATIVE_WRITER` must be declared in
  `app/services/sot_registry/`. An owner nobody declared is a parallel writer
  with a flattering name.
- A surface classified `UNKNOWN` may not name an owner, and `UNKNOWN` never
  means "none found". A searched-and-empty result is recorded separately, with
  the search's blind spots stated.
- The writer baseline moves in one direction per change and only
  deliberately: a new or grown writer fails, and so does a removed writer
  whose line was not lowered in the same change.
- The export path writes nothing, commits nothing, and decides nothing about
  the destination.

## Consequences

- The cohort's real debt is now a number a reviewer can check rather than an
  impression: 26 production writers, of which 18 are parallel writers of a
  fact some other owner is declared to own.
- New Sub work touching party, customer or brand-profile rows now meets a
  guard. That is friction, and it is the intended kind: ADR 0012 in Governance
  already bars permanent new Sub domain logic for this cohort.
- The baseline will look "wrong" for a long time. It is a debt ledger, not a
  boundary, and it stays at 26 until real cutover pull requests lower it.
- Rejected alternative: classify writers by directory. Sub writes cohort rows
  from `app/services/web_*.py` presenter modules, from `scripts/one_off/`, and
  from applied migrations; a directory-scoped guard would have stated an
  unenforceable premise and reported a comfortable, wrong number.
- Rejected alternative: derive the writer list at cutover time. The set is not
  stable — it grew while this was being written — so it has to be frozen
  before, not measured after.

## Migration and cutover

- Old owner and paths: unchanged. Sub's declared owners keep writing.
- New owner and paths: none. This ADR moves no authority.
- Backfill/repair: none. The export path is read-only.
- Shadow or verification phase: not started. The digest contract exists so a
  future `asm-dotmac-isp` shadow importer can compare without Sub database
  access; no comparison has been run.
- Cutover gate and evidence: Governance `ctl-isp-006`, `ctl-isp-007` and
  `ctl-isp-009`. This work produces *inputs* to those controls. A control is
  verified only in Governance, citing an immutable controlled-source
  reference; nothing in this repository advances one.
- Fallback retirement: the ratchet baseline is lowered writer-by-writer in the
  cutover pull requests that remove them, after the sealed authority switch.
- Schema contract step: none. No migration is added.

## Verification

- `tests/architecture/test_isp_cohort_source_writers.py` — the ratchet in both
  directions, the inventory/census coherence checks, and five sensitivity
  proofs: a new direct writer, an adapter bypassing its owner, raw SQL, an
  unscanned repository root, and the acceptance case that an unrelated
  attribute assignment is not counted.
- `tests/test_isp_cohort_source_readiness.py` — the programme binding refuses
  a mutable revision, the cohort surface refuses duplicates, and the
  classification validators refuse an undeclared owner and an UNKNOWN that
  names one.
- Snapshot and digest verification is listed in
  `docs/ISP_COHORT1_SOURCE_OWNERSHIP.md`.

## Rollback or forward-fix

Everything here is additive and read-only; deleting the package and the guard
restores the previous state exactly. There is no data change to reverse. The
only durable consequence of a mistake is a wrong classification, which is a
forward-fix in the same file.

## Amendment — 2026-08-22: the binding is repinned

The Context above cites `68c7a62e…`, the revision that ACCEPTED the programme.
That remains the correct history and is left as written. The binding in
`app/migration_source/programme.py` now pins
`d91a87f6823bfd2afa6c2025bdb1af644331fa39` instead — the revision that answered
dec-isp-003 through dec-isp-007 and added `dotmac-addresses` to cohort 1.

A pin is not a citation. The Context says where the programme was accepted; the
pin says which revision's cohort definition Sub is transcribing, and pointing it
at a revision predating five answered decisions and a sixth component described
a cohort that no longer existed. The binding also gained
`resolved_decision_ids`, because Sub's view could previously say only what was
still open, so an answered decision left no trace and the record read as though
nothing had been settled.

## Amendment — 2026-08-21: three axes and a disposition

The inventory's single `SurfaceClassification` was answering three independent
questions at once, and the collisions were not edge cases. An applied migration
is not an "authorized adapter"; calling it a "legacy parallel writer" reads as
something a cutover could retire. A fixture seeder writes real rows and holds
no authority at all. Both had to be fudged.

The inventory now classifies on three orthogonal axes — `AuthorityRole` (what
say it has over the fact), `BoundaryRole` (what it does there), and
`Reachability` (how it can still be reached against production) — and
`SurfaceClassification` survives as a *derived* view, so every document and
control record written against the original eight names still means what it
meant, with no second place to edit.

Orthogonality is proved rather than asserted: for every ordered pair of axes,
knowing one value must leave at least two possibilities open on the other,
measured over the real inventory. If that ever collapses, the honest fix is to
merge the axes, not to invent a surface that keeps them apart.

Every surface — including the adapters and readers that write nothing — now
also carries a `Disposition`: what becomes of its cohort-1 touch when authority
moves. `UNDECIDED` is a real state with a required `open_question`, and three
surfaces hold one. They are enumerable, because a question that lives only in
prose is one somebody answers by accident.

Two cross-axis invariants are worth naming because they were the fudges:

- a surface that persists cohort state against a reachable database cannot
  claim `NO_AUTHORITY` — writing is authority whether or not anybody granted
  it — but one that persists only to a disposable database can;
- a surface reachable only as an applied migration holds the schema lineage's
  authority and nothing else.

## Amendment — 2026-08-21: no online export adapter

The export is reachable through one adapter, `scripts/migration/
export_isp_cohort_snapshot.py`, and deliberately not through an HTTP route.

An authenticated `/api/v1` export endpoint would be a standing, network-
reachable egress surface for the customer identity data of the whole cohort.
It would have no consumer: `ctl-isp-002` is open, so no destination is named
and no deployment owner exists. Permanent exposure against zero current use is
the wrong trade, and "we will need it eventually" is not a reason to open it
now.

The operator CLI has the properties an online route would have to re-earn:
access is host access rather than a bearer token, the full snapshot writes a
0600 file and refuses a terminal, the default mode carries no field values at
all, and every invocation is a deliberate act on a host somebody already had
to reach.

Conditions for revisiting, all of which must hold together: `ctl-isp-002`
verified with a named destination and deployment owner; the destination
authenticating as a specific principal rather than a shared token; a dedicated
permission no seeded role carries by default; egress restricted to the
destination; and per-export audit carrying contract version, checkpoint and
requesting principal. `docs/ISP_COHORT1_MIGRATION_READINESS.md` § "Why there
is no online export endpoint" is the operative statement.

## Review and retirement

- Review date: when `dec-isp-002` and `dec-isp-003` are decided in Governance,
  or when the cohort-1 component releases are pinned, whichever is first.
- Retirement condition: the cohort's sealed authority switch completes, its
  displaced writers reach a bidirectional ratchet of zero, and the legacy
  paths are deleted after the rollback window.
- Supersedes or is superseded by: none. Related: ADR 0011 (module lineage
  composition) — a rehearsal of composing a module lineage is not adoption,
  and neither this ADR nor that one composes a Starter module.
