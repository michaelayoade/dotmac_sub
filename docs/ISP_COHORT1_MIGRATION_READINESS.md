# Cohort-isp-01 migration readiness runbook

Preparation for converting Sub in place from `asm-dotmac-sub-legacy-domains`
to `asm-dotmac-sub-composed`. Both assemblies are `dotmac_sub`: one product,
runtime, API and database (`shared-in-process`). The separate `dotmac-isp`
walking skeleton is frozen and is not the cutover target.

**Nothing in this document is authorised to run.** No authority moved, no
module was installed, and no migration, backfill, shadow or switch was run by
this binding update. No host, date or operational window is selected. Controls
remain in their accepted Governance states until their own evidence is accepted.

Companion documents: `docs/adr/0012-isp-cohort-source-readiness.md` and
`docs/ISP_COHORT1_SOURCE_OWNERSHIP.md`.

## Where the source stands

| | |
|---|---|
| Programme | `pgm-dotmac-isp-replacement` at `dbd9cec107ee5125a8d41f75065d3823f69d6b8e` |
| Controlled record | `programmes/dotmac-isp-replacement.json` in Governance |
| Cohort | `cohort-isp-01`, sequence 1, state **blocked** |
| Source | `asm-dotmac-sub-legacy-domains` — source-authoritative |
| Target | `asm-dotmac-sub-composed` — same Sub runtime and database |
| Entity types | 12 |
| Production writers | 28 (11 declared owners, 2 derived projections, 15 parallel) |
| Contract version | 1 |
| Record schema version | 1 |

The source-readiness claims remain `inventoried`, `export_contract_ready`,
`digest_contract_ready`, and `writer_surface_ratcheted`. They establish source
tooling, not actual module adoption, population backfill or switch acceptance.
Historical snapshots and digests are evidence only for their recorded source
revision. Explicit historical `SourceRevision` values remain parseable and
retain reproducible digests; new defaults use the current assembly and pin.
Do not rewrite old provenance or compare different provenance as if identical.
The comparator rejects different source assembly IDs or Governance revisions as
`unsupported-version`; retained historical golden fixtures preserve the old
aggregate rather than restamping it.

There are no open programme decisions. In particular, `dec-isp-002` and its
`ctl-isp-002` are superseded, not destination-host procurement blockers.
`ctl-isp-003` remains blocked despite the answered transition decision.

## Current Governance controls

| Control | State | Meaning for preparation |
|---|---|---|
| `ctl-isp-001` | verified | Attributable programme approval; not operational approval |
| `ctl-isp-002` | superseded | Separate destination deployment-owner requirement retired |
| `ctl-isp-003` | blocked | Enforce bounded legacy Sub transition work |
| `ctl-isp-004` | blocked | Exact verified artifacts and composed migration graph |
| `ctl-isp-005` | superseded | Separate target database rehearsal retired |
| `ctl-isp-006` | blocked | Explicit row dispositions and idempotent backfill |
| `ctl-isp-007` | superseded | Cross-application shadow requirement replaced by 011 |
| `ctl-isp-008` | superseded | Cross-application switch requirement replaced by 012 |
| `ctl-isp-009` | blocked | Zero displaced writers before rollback-window closure |
| `ctl-isp-010` | blocked | Sub database module-lineage rehearsal on a production-shaped copy |
| `ctl-isp-011` | blocked | In-process read-only shadow against the same database |
| `ctl-isp-012` | blocked | Operator-approved per-domain one-writer switch and config rollback |

A control is verified only in Governance with immutable controlled-source
references. This repository neither verifies controls nor opens the cohort.
Independent modules need their own complete dependency and capability evidence;
they do not wait for unrelated whole-cohort gaps to close. Whole-cohort closure
still accounts for all twelve entity types and six components: Addresses,
Brand Profiles, Customers, Kernel, Party and UI.

## Preparation and switch sequence

### 1. Select the module and verify its exact artifact graph — `ctl-isp-004`

Select one bounded module/concern and record the exact compatible dependency
graph: immutable source commits, artifact filenames, registry-readback SHA256,
permanent verification receipts and migration heads. Tags or versions alone
are insufficient. Sub is now the assembly that composes these modules; the old
prohibition on composition in Sub is superseded by in-place conversion.
Composition disabled is preparation, not adoption.

Current evidence frontier (Starter source `0aa465446794ae03abc743f9e288e9f3831be3aa`):

- Kernel `0.1.0a104` has a verification receipt for source
  `bf16e30efc3321ca9a495de31570bb5f9a02e9e3`. However, committed records name
  the same `dotmac_kernel-0.1.0a104-py3-none-any.whl` with two SHA256 values:
  kernel receipt `183e0aa003db22fc2cfd8a846df14589027e34a9632180b018222d5eac0d6076`
  and module dependency ledger
  `54562973a659427aed067a504aa0eaaa427fda8e13d184e8401d72d234ec2534`.
  These records have different build provenance: `release-module.yml` builds
  a local smoke dependency wheel without generating CSS, whereas
  `release-kernel.yml` generates CSS before building the released wheel.
  Generated `static/css/main.css` is included in the wheel; the kernel changelog
  documents repository-wide Tailwind inputs. This recipe difference explains
  why identical kernel source/version need not imply identical wheel bytes;
  it is not proof of either retained file's exact contents or a registry defect.
  Do not substitute the smoke hash for the release receipt. Establish retained
  bytes and test compatibility against the exact published dependency before
  declaring the selected graph verified. Module-run artifact metadata returned
  404 through the available connection, so smoke-artifact retention is unknown.
  Do not rebuild or replace the existing a104 tag.
  Unpublished `0.1.0a104+dev` is not a substitute or a universal prerequisite.
- UI `0.1.0a8` has a historical release record and bundle SHA256
  `1997fe0ca07c058056cefc6ef8a01d4355c4c9c95d678e251b4b2ae08b8abd24`;
  that record does not supply an individual wheel digest. Normalize retained
  evidence and independently read back the file; this alone does not require
  a new release.
- Party, Brand Profiles and Customers historical a1 tags are explicitly
  legacy-unverified/non-pinnable in the current ledger. Historical publication
  statements cannot replace retained registry-byte proof.
- Addresses has no tracked owner package in the inspected Starter package tree.
  Registry availability remains unknown, not proven absent. Locate or implement
  the approved product-first owner when that module or cohort closure needs it.

Evidence paths in Starter are `docs/inventories/kernel-release-verifications/0.1.0a104.json`,
`docs/inventories/release-record-kernel-a99-ui-a8.md`,
`docs/inventories/module-release-verifications.json`, and
`docs/inventories/module-release-legacy-unverified.json`. This runbook does not
claim artifact readback occurred or authorize publication. A successor release
requires its own authorization; existing immutable tags are never rebuilt.

The build-recipe explanation is source evidence at the inspected Starter
revision: `.github/workflows/release-module.yml`,
`.github/workflows/release-kernel.yml`, root `package.json`, and
`packages/dotmac-kernel/{pyproject.toml,CHANGELOG.md,scripts/verify_static_artifact.py}`.
No artifact bytes were downloaded for this assessment.

### 2. Bind selected source concerns to actual module contracts — `ctl-isp-006`

For every entity, relation, writer and caller in the selected slice, declare one
outcome: migrate through a named typed owner contract, explicitly retain with a
named product owner, or retire with its writer. Coverage is total and disjoint
for that slice. Keep source identities and unresolved correlations; do not
invent generic metadata ownership or infer capabilities from package names.

The source entity table is an expectation, not capability closure. Kernel
already implements Party/person/organization identity; the missing proof is
its explicit mapping/link contract. Party excludes person/organization identity
and customer accounts; Customers excludes identity, reachability and addresses.
Map `customer_contact`, `organization` and `organization_membership` explicitly
rather than assigning them to Customers because of the historical table label.
Addresses draws on Sub's four declared address owners. A missing Addresses
package blocks the relevant slice and eventual cohort closure, not an unrelated
module with complete dependencies.

The fifteen parallel production paths are switch-time retirement entries,
not fifteen automatic pre-cutover refactoring PRs. Earlier source edits need a
concrete backfill/shadow/switch blocker, a path deliberately remaining live,
or an immediate security/data-loss defect. The legacy disposition label
`ROUTE_THROUGH_OWNER_FIRST` does not impose blanket cleanup before shadow.

### 3. Rehearse the real module lineage inside Sub — `ctl-isp-010`

On an explicitly authorized hosted disposable PostgreSQL/PostGIS target made
from a production-shaped Sub copy, compose the selected verified graph with
writers disabled. Record the source application revision, Sub Alembic heads,
module lineage heads, exact artifact graph and copy watermark. Use the real
Sub predecessor-to-head migration chain and module lineages; fresh-baseline
checks alone, SQLite and `Base.metadata.create_all()` do not prove deployed
schema compatibility. Check catalog, constraints, grants and tenant isolation
where the selected module requires them. Capture before/after counts, failures
and immutable receipts. Never use local Docker or local PostgreSQL for this
rehearsal. This document supplies no target or execution authorization.

### 4. Backfill or quarantine idempotently — `ctl-isp-006`

Stage the approved selected-module mapping on that rehearsal copy. Record each
row's migrated, retained or retired outcome; unresolved/invalid rows go to a
named quarantine with reason, source identity, repair owner and retry contract.
Quarantine is accounted-for evidence, not silent omission or permission to
switch with unresolved invariants.

**Backfill ordering.** For whole-cohort accounting, the source dependency order
is below; use only the selected slice and its required dependencies:

1. `party`.
2. `party_role`, `party_external_reference` (reference `party`).
3. `organization` (references `party`; its `parent_id` needs a second pass or
   approved deferred constraints).
4. `brand_profile` (scope can refer to reseller or organization).
5. `customer_account` (references party, organization and later-cohort IDs:
   `reseller_id`, `tax_rate_id`, `policy_set_id`, `sales_order_id`).
6. `party_relationship`, `party_membership`, `party_contact_point` (party links).
7. `organization_membership` (organization and person links).
8. `customer_contact`, `customer_address` (customer-account links).

These rows already inhabit Sub's shared database. In-process composition does
not imply their referenced IDs are absent or require conversion of existing
foreign keys to correlations. Explicitly map later-cohort links according to
the selected contract; retain unresolved correlations without dropping them.

**Deduplication keys.** `<entity_type>:<uuid>` is the stable source identity.
Never deduplicate accounts by email, phone or subscriber number. Shared contact
addresses are valid. Replay the same watermark twice and prove unchanged target
state/digest and row-disposition receipts; then rehearse retry and delta
convergence without duplicate records or repeated business side effects.

**Checkpoint and consistency.** Export pagination remains keyset
`after_source_id`. A complete drain uses one REPEATABLE READ, READ ONLY
transaction and one `source_revision` carrying Alembic head, application version
and PostgreSQL visibility snapshot. Different `snapshot_transaction_id` values
must not be combined. Resume only within the same fixed snapshot/revision;
otherwise restart the drain. Existing exporter tests prove a contract, not an
actual population rehearsal.

**Observations and derived facts.** Source snapshots are observations, not new
module decisions. Recompute `status`, `is_active`, `lifecycle_override_status`,
`lifecycle_override_reason`, `lifecycle_override_source`, `lifecycle_override_at`,
`legacy_party_status` and `mrr_total` through their actual owning policies and
inputs. Do not adopt those projections as customer authority. `metadata`,
`access_scope` and `legal_address` remain key inventories plus digests, never
invented typed facts. The snapshot adds no target status/disposition vocabulary.

### 5. Run read-only in-process shadow — `ctl-isp-011`

With legacy authority still active and candidate writers disabled, compare the
selected module's candidate read/resolver output with the authoritative source
inside the same Sub database at a consistent recorded watermark. Shadow must
not write, dispatch side effects or become a fallback writer. Capture the exact
mapping, graph, row counts, completeness and explanation for each mismatch.
The existing `CohortDigest` and `digest.compare` remain bounded comparison tools;
identity/hash artifacts do not prove a shadow run actually happened.

| Verdict | Response |
|---|---|
| `missing-from-target` | Repair mapping/backfill or account for the row through its approved disposition |
| `unexpected-in-target` | Identify provenance and exclude an unintended writer or mapping error |
| `divergent` | Investigate canonical fields, normalization and declared derived semantics |
| `unsupported-version` | Produce comparable contract/provenance versions; never coerce a match |
| `source-unknown` | Complete the source drain |
| `target-unknown` | Complete the candidate drain |

Require complete scope and zero unexplained drift, or row-specific human
adjudication naming identity, reason and invariant impact. There is no automatic
acceptable-difference verdict. A writer remaining live during shadow is an
explicitly modeled source input or a demonstrated blocker; its census label
alone does not require an advance refactor.

### 6. Rehearse a safe one-writer config rollback — `ctl-isp-012`

On the authorized rehearsal copy, test switch and rollback for the selected
module/concern. Record the exact configuration, owner admission controls and
state watermark. Prove that config selects exactly one writer and excludes
legacy fallbacks in either direction. A config flip is safe only when the
returning owner can interpret all post-switch records and preserves audit,
idempotency and outbox semantics. If that compatibility cannot be proved,
stop new writes and use the approved reconciliation/forward-fix path; do not
restore a stale database or enable both owners. Test post-switch writes and
retry handling, not just an empty configuration flip.

### 7. Obtain operator approval and switch the selected authority — `ctl-isp-012`

A later attributable operator approval must name the module/concern, exact
graph, immutable rehearsal and shadow receipts, target host, config transition,
per-slice in-flight transaction fencing/quiescence, rollback conditions and
observation window. This
runbook is not that approval. Measure traffic-zero for the paths displaced by
this slice and account for in-flight work. Reconcile source changes since
backfill under the same-database consistency contract before admitting the new
writer. No cross-database delta capture or whole-product traffic drain is
required by this in-place switch.

Redirect or disable every displaced writer and fallback in the same selected
module authority-switch slice. Architecture and database controls must prevent
parallel authority from returning; update both ratchets deliberately with
retirement. Do not postpone disabling paths until cohort-wide closure. Preserve
unrelated responsibilities in multi-purpose files, such as subscription
lifecycle in `account_lifecycle.py` or non-cohort account-recovery consequences.

### 8. Prove retirement before closing the rollback window — `ctl-isp-009`

Require zero displaced writers for the selected switched scope, with path-level
traffic and admission evidence, updated baselines and guards. Retained rollback
code is disabled, explicitly gated and never a live fallback. Delete retired
paths when the approved rollback window closes. Whole-cohort closure eventually
accounts for all 28 currently reachable production writer surfaces; the 15
parallel paths are part of that ledger. Nine applied migrations and two
non-production tooling entries remain historical/non-production ratchet entries;
they are not live authorities to retire. Independent module acceptance is not
held until all unrelated cohort writers reach zero.

## First preparation candidate: Brand Profiles

This is the first bounded packet to complete, not a claim of adoption or an
approved switch. Source evidence is Sub `b69f1b4a6d088522ce5834aa57ccd94a3530606d`;
module evidence is Starter `0aa465446794ae03abc743f9e288e9f3831be3aa`.
The scope is the `brand_profile` entity (`brand_profiles`, `BrandProfile`) and
`customer.branding`'s profile commands and display resolver. It excludes legal
identity, billing-company facts and email legal-sender authority. Replacing a
branding read must not transfer those other owners into Brand Profiles.

Compose the selected owner on the TENANT plane in the existing Sub database.
Sub's source scope named `platform` is a candidate tenant-wide default, not
permission to compose the platform control-plane profile/host-binding lineage.
`customer.branding` depends on `customer.identity_scope` and
`control.domain_settings`; prove the relevant relation contracts without
requiring unrelated Party/Customers cohort completion.

### Mapping decisions and evidence still required

| Source concern | Required target contract or disposition | Status |
|---|---|---|
| Source UUID and platform/reseller/organization scope | Stable source correlation; explicit tenant default and reseller/organization identity links; source `scope_id` has no FK | open |
| Brand/product/legal names and active flag | Map to required `profile_code`/`display_name`, field provenance/locks and DRAFT/ACTIVE/RETIRED semantics without inventing legal authority | open |
| Organization > reseller > platform > legacy resolution | Prove target selection and per-field fallback precedence for every source scope; no legacy fallback after switch | open |
| Primary/accent colors | Map typed values and compare rendered output against the released UI token contract | open |
| Five `metadata.semantic_colors` overrides | Dossier classifies these as `OWNED_BY_PUBLISHED_TOKEN`; enumerate actual values and explicit unsupported-value dispositions | open |
| Logo, favicon and other asset URLs/static paths | Map to opaque file references owned by the file capability; prove byte/reference and deletion behavior rather than assuming URL equivalence | open |
| `tagline`, `app_url`, `portal_domain` | No direct target tenant-profile fields established; assign typed retention, migration or retirement outcomes, never silently drop | open |
| Sender/support fields and `legal_address` | Map each field's actual meaning and owner; existing opaque-address evidence is not a structured address contract | open |

The inspected module has different tenant/platform models and migration
prerequisites, including tenant scope catalog, idempotency and database roles.
Record exact plane selection and dependency lineage from the chosen verified
release; source compatibility is not demonstrated merely by the package name.
The historical a1 release is currently non-pinnable. No exact artifact graph
or migration/backfill/shadow acceptance is established for this candidate.

### Switch-time path checklist

Confirm the exact call graph on the eventual candidate commit. The current
packet records these paths rather than scheduling source cleanup beforehand:

- `app/services/brand_profiles.py`: replace upsert/sync/deactivate commands and
  disable `_legacy_brand` fallback once module parity and rollback are proved.
- `app/api/branding.py`: route PUT/DELETE adapters through the selected owner.
- `app/web/admin/system.py`: the branding settings handler writes comms settings,
  removes managed assets and syncs the profile; separate its retained concerns
  and retire the displaced brand writes in the switch slice.
- `app/services/web_system_company_info.py`: preserve company-info ownership,
  replace its profile sync consequence and prove transaction behavior.
- `scripts/utils/settings_sync.py`: classify and disable/repoint branding-key
  writes while preserving unrelated settings synchronization.
- `scripts/one_off/backfill_brand_profiles.py`: gate or retire the legacy
  backfill entry point so it cannot reintroduce a writer after the switch.
- `alembic/versions/267_brand_profiles.py`: preserve applied schema history;
  it is not a live writer to refactor or delete.

Reader census and parity must include customer/reseller/public/portal branding,
admin/auth contexts and `app/api/me.py`; invoices, sales quotes, support,
notifications/tasks, inbox projections and catalog presentation. Inspect direct
bypasses in `app/services/email.py`, invoice logo settings/StoredFile fallbacks,
theme/static/favicon lookups, and direct profile-table reads. Repoint only the
profile concern; retain each unrelated fact's actual owner. This grouped list
is a starting inventory, not proof that all callers have been enumerated.

### Acceptance receipts for this candidate

Keep these incomplete until immutable evidence exists: verified artifact and
compatible dependency graph tested against the exact released kernel bytes;
total field/scope/file dispositions; TENANT composition and hosted
real-lineage PostgreSQL rehearsal; idempotent backfill/quarantine with source
correlations; profile-only same-database shadow at consistent provenance;
one-writer admission and in-flight fencing; post-switch data-compatible config
rollback; zero displaced profile writers/fallbacks with architecture/database
guards and path-level receipt. Independent acceptance does not require all
twelve cohort entities to migrate. No build, install, production read or switch
is authorized by this packet.

## Remaining source evidence

No open programme decision is implied by an unfinished evidence task:

- Explicit mapping/link and disposition receipts are needed for contacts,
  organizations and memberships; zero counted writers is not no data.
- The recovery cascade crosses invoices, payments, credentials, RADIUS, IP
  and ONT assignments. A selected Customers switch must preserve those owners
  and retire only its displaced account touch; demonstrate separability when
  that slice needs it, not as a prerequisite for every module.
- Opaque metadata remains evidence. Source address corruption needs measured
  repair evidence where it affects the selected module; no sweep is authorized.
- `subscriber_nin_verifications` remains excluded with Compliance/Records
  retention evidence, and the other deliberately unmapped adjacent tables keep
  their declared reasons. No purge is authorized.
- `mrr_total` recomputation must be proved where its owning slice switches.

## Operating the export

Both modes read only and refuse a tenant other than the operator tenant.
These invocations document the existing adapter; they are not authorization
to access a host or customer population.

```bash
# comparison digest: identities and hashes, no field values
python scripts/migration/export_isp_cohort_snapshot.py --digest

# one page; refuses a terminal and writes a 0600 file
python scripts/migration/export_isp_cohort_snapshot.py \
    --snapshot --entity-type party --page-size 200 --out /path/to/party-0.json
```

Snapshots contain customer data. Use digest artifacts for comparison evidence
where values are unnecessary, and keep the recorded source provenance intact.

## Why there is no online export endpoint

In-place module composition has no cross-application export consumer. The
operator CLI and in-process typed read boundary cover the current preparation
contract; a standing network egress endpoint would add a separate access
surface. `ctl-isp-002` is superseded and cannot be used to require or unlock
such an endpoint. A future justified remote consumer requires an explicit
architecture decision, named principal, dedicated permission, restricted egress
and audited contract version/checkpoint/principal. No HTTP adapter is added here.
