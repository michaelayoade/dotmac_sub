# ADR 0012: Sub is the measured source for ISP replacement cohort 1

Status: accepted

Date: 2026-08-21

Decision owner: Michael Ayoade

Affected systems and domains: party_identity, customer_context, the
cohort-isp-01 persistence surface, and Sub in-place module composition

## Implementation alignment with accepted Governance — 2026-10-13

This section implements the already accepted
[Governance conversion amendment](https://github.com/michaelayoade/dotmac_governance/blob/dbd9cec107ee5125a8d41f75065d3823f69d6b8e/docs/adr/0012-dotmac-isp-replacement-programme.md#L246),
not an independent architecture acceptance. The authoritative programme is
`programmes/dotmac-isp-replacement.json` at Governance
`dbd9cec107ee5125a8d41f75065d3823f69d6b8e`. Source
`asm-dotmac-sub-legacy-domains` and target `asm-dotmac-sub-composed` both name
`dotmac_sub`, with database boundary `shared-in-process`. Sub retains its
product, runtime and API. The separate ISP walking skeleton is frozen.

This implementation alignment supersedes the earlier separate-application and
separate-database assumptions and destination-owner prerequisites below. The original decision and amendments
are retained as historical source-tooling evidence, including their historical
counts, assembly IDs and pins; they are not current operational instructions.
The six cohort components remain Addresses, Brand Profiles, Customers, Kernel,
Party and UI. No programme decisions remain open. `dec-isp-002` and controls
002/005/007/008 are superseded. `ctl-isp-001` is verified; controls
003/004/006/009/010/011/012 and cohort 1 remain blocked.

Sub's read-only source snapshot, digest, typed contracts, census and ratchets
remain useful preparation. `SourceReadinessClaim` still cannot claim adoption
or cutover. New source revisions default to the current assembly and Governance
pin; explicit historical `SourceRevision` values remain parseable with their
original canonical digest interpretation. Existing snapshots/digests prove
source tooling and historical provenance, not an actual in-place adoption. The
comparator rejects different source assembly IDs or Governance revisions as
`unsupported-version`; historical golden fixtures preserve the old aggregate.

The current sequence is exact verified graph selection and selected-module
capability mapping, real Sub/module lineage rehearsal on hosted disposable
production-shaped PostgreSQL/PostGIS (`ctl-isp-010`), idempotent row dispositions
and backfill/quarantine (`ctl-isp-006`), same-database read-only in-process shadow
(`ctl-isp-011`), and operator-approved per-domain one-writer config switch with
safe rollback (`ctl-isp-012`). No local Docker/PostgreSQL rehearsal is permitted.
See `docs/ISP_COHORT1_MIGRATION_READINESS.md` for evidence and ordering.

Retire each selected module's displaced writers and fallbacks in the same
authority-switch slice. The fifteen parallel paths are retirement ledger
entries, not fifteen prerequisite cleanup PRs. Earlier edits require an evidenced
backfill/shadow/switch blocker, a path deliberately remaining live, or an
immediate security/data-loss defect. Independent modules whose own contracts and
dependencies are complete do not wait for full cohort closure. `ctl-isp-009`
requires zero displaced writers before the applicable rollback window closes.
Safe config rollback must preserve one writer and interpret post-switch data;
it must never restore parallel authority or a stale database.

Artifact verification remains a concrete gap, not an adoption claim. Committed
kernel a104 records give the same wheel filename two SHA256 values:
`183e0aa003db22fc2cfd8a846df14589027e34a9632180b018222d5eac0d6076` in the
kernel receipt and
`54562973a659427aed067a504aa0eaaa427fda8e13d184e8401d72d234ec2534` in the
module dependency ledger. Source tracing distinguishes the module workflow's
local smoke build (without CSS generation) from the kernel release build
(generated CSS included). The documented build inputs can explain different
bytes under the same filename; exact retained-file contents remain unverified.
Do not substitute the smoke hash for the release receipt or infer a registry
defect. Verify the exact released dependency and its compatibility; neither
this finding nor unknown smoke-artifact retention permits rebuilding the tag.
See the runbook's build-provenance references. Party/Brand/Customers a1
are historical legacy-unverified/non-pinnable; Addresses has no tracked owner
package, with registry availability unknown. Select a compatible verified graph
for the chosen slice rather than requiring an unrelated unreleased dependency.

**No authority moved and no operation is authorized by this alignment.** No
module install, migration, backfill, shadow, retirement or switch is performed.
Only Governance can advance controls with immutable accepted evidence.

## Historical decision — 2026-08-21

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

## Historical review and retirement criteria

The current alignment above supersedes the open-decision triggers and the
post-switch-only retirement ordering in this historical section.


- Review date: when `dec-isp-002` and `dec-isp-003` are decided in Governance,
  or when the cohort-1 component releases are pinned, whichever is first.
- Retirement condition: the cohort's sealed authority switch completes, its
  displaced writers reach a bidirectional ratchet of zero, and the legacy
  paths are deleted after the rollback window.
- Supersedes or is superseded by: none. Related: ADR 0011 (module lineage
  composition) — a rehearsal of composing a module lineage is not adoption,
  and neither this ADR nor that one composes a Starter module.
