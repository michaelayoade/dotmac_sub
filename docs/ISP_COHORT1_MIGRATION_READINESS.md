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
approved switch. Mapping/census evidence is Sub `0ab045878aaa9df8144b8a380cc4d210a57fe9ef`;
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

### Pinned source contract and candidate field mapping

This is a source-code census, not a population census or approved migration
contract. All 23 mapped columns of `app/models/branding.py::BrandProfile` are
covered below. The source export is
`app/services/migration_source_export.py::_brand_profile_record` and
`app/migration_source/snapshot.py::BrandProfileRecord`; its `source_id` is the
source row UUID. `legal_address` and `metadata_` cross that boundary as
`OpaqueBlob` key inventory/digest, so the existing export cannot reconstruct
those values or enumerate actual semantic overrides for a backfill. Their
value-bearing, typed extraction and provenance remain an acceptance gate.

Target paths below are under
`packages/dotmac-brand-profiles/src/dotmac_brand_profiles/` at the Starter SHA
above: `models.py` defines storage, `ports.py::ProfileFields` defines inbound
values, `service.py` defines commands/resolution, and `brand_values.py` defines
color translation. A matching column is a candidate destination, not a grant
of legal, support-routing or sender authority.

| Source column(s) | Candidate destination / explicit unresolved outcome |
|---|---|
| `id` | Stable source correlation to target UUID returned by `upsert_tenant_profile`; that command allocates its own UUID and accepts no source UUID. Approve a durable correlation owner/key and collision policy; do not claim identity preservation. Candidate deterministic `profile_code = sub-brand-<source UUID>` fits 120 chars but remains unapproved. |
| `scope_type`, `scope_id` | Target `tenant_id` plus registered scope chain. Source `platform` with null ID is a tenant-default candidate; reseller/organization UUIDs need explicit identity links/existence checks. No cross-lineage FK or invented sentinel. Scope spelling and tenant assignment require approval. |
| `brand_name` | `display_name` (source 120 chars, target 160). Source nullable/empty inherits, target required: approve fallback/materialization with provenance; never expose generated `profile_code` as an accidental display name. |
| `product_name` | `product_name` (160). Preserve null/empty inheritance and source company-name fallback semantics; explicit clear is blocked below. |
| `legal_name` | `legal_name` (200), as presentation of the existing legal fact only. Company-info/identity owner and permitted scope overrides must be declared; importing a lock list does not establish legal ownership. |
| `tagline` | No `ProfileFields` destination. Explicit typed product retention or approved retirement required; no silent drop and no generic metadata bucket. |
| `primary_color` | `primary_hex` → assembly maps to `dotmac_ui.BrandOverride.primary`. Same hex value does not imply same ramp/rendered result. |
| `secondary_color` | `accent_hex` → `BrandOverride.accent`. An accent-only source layer is valid today but target validates each command payload as requiring primary alongside accent; approve a parity-preserving translation/provenance strategy. |
| `logo_url`, `dark_logo_url`, `favicon_url` | `logo_file_ref`, `dark_logo_file_ref`, `icon_file_ref` (200 each). Each source URL class needs a file-owner disposition and correlation; a URL or `StoredFile` UUID is not proven to be a target file reference. See asset matrix. |
| `support_email`, `support_phone` | Same target names (255 / 40). Retain actual support owner and inherited company-info provenance; scope policy remains open. |
| `from_email`, `from_name` | `sender_email`, `sender_name` (255 / 160), presentation only. Connector authorization, SMTP configuration and consent stay with their current owners. |
| `app_url` | No equivalent tenant-profile field. Do not rename to `support_url`: application origin and support endpoint have different meanings. Declare typed product retention and origin-owner contract. |
| `portal_domain` | No equivalent tenant-profile field. Do not create platform host bindings or bypass Kernel host-to-tenant ownership. Declare product retention/link/retirement outcome. |
| `legal_address` | Target `legal_address` exists as JSON, not a structured address contract. Source applies a nonempty object as a whole replacement. Existing exporter carries only keys/digest. Approve value-bearing shape/provenance and address/legal owner; no inferred transfer to Brand Profiles. |
| `metadata_` | No generic target metadata field. Known `semantic_colors` is addressed below; every other measured key requires explicit typed retention or approved retirement. Existing key/digest export is insufficient for a value census. |
| `is_active` | Target DRAFT/ACTIVE/RETIRED state requires a lifecycle contract. New upsert is DRAFT, activation is separate; no tenant retirement command is exposed. Source upsert reactivates, source DELETE deactivates. Do not equate inactive with permanently retired or draft without approval. |
| `created_at`, `updated_at` | Target timestamps exist, but public upsert exposes no historic timestamp input. Preserve source timestamps in approved migration provenance or obtain an explicit historical-time disposition; do not silently replace history with migration time. |

Target-only `support_url`, `enabled_surfaces`, `default_locale`,
`default_timezone`, `mobile_build_profile_ref`, `locked_fields` and
`record_version` have no direct source column. Leave optional values unset until
an owner supplies them; do not infer locale, signing profiles or legal locks.
`record_version` is target mutation history, not a source timestamp/version.

The five known semantic keys are `positive`, `info`, `warning`, `negative` and
`neutral`. They occur in `metadata_["semantic_colors"]`, legacy comms
`brand_semantic_<tone>_color`, and deployment-static
`semantic_<tone>_color`. Starter `EXTRACTION.toml::shadow_and_drift` records
Michael's 2026-08-19 ruled reduction: UI publishes these tokens and profile
values are not carried. Flatten each value into
`translate_legacy_brand_values` and retain its `UnsupportedBrandValue` with
`OWNED_BY_PUBLISHED_TOKEN`, source row/scope and reviewed rendered difference.
This ruling does not authorize dropping unrelated metadata or treating a
key-only digest as an actual-value receipt. No population values were read.

### Scope, resolver and command compatibility gates

Source constraints allow only `platform`, `reseller`, `organization` with
null ID iff platform, and one row per scope regardless of active state.
Source upsert checks reseller/organization existence. Resolution infers both
links from `Subscriber`, unless explicit IDs were supplied, then applies legacy,
platform, reseller and organization in that order. Scalars override only when
neither null nor empty; nonempty legal address replaces the whole object;
semantic colors merge by recognized tone. Source `source_scope` records the
last applied profile even when it contributes no scalar, rather than each
field's real provenance. Preserve the externally consumed provenance contract
or approve an explicit compatibility change.

The proposed target chain is organization, reseller, tenant default, all on
TENANT with an already resolved real tenant. Exact default scope spelling is
open. `resolve_for_tenant` selects ACTIVE rows, then `resolve` takes first
non-null per field and reports per-field `sources`. Prove these differences
against the selected release before composition:

- Target empty strings/empty address objects count as supplied; source empty
  strings/empty objects inherit. Upsert `ProfileFields.as_dict` removes `None`
  (leave unchanged), while source upsert assigns an explicitly supplied `None`
  or empty value. Define typed unset/clear/inherit outcomes for create/update
  and admin asset removal; blanking a value is not a safe substitute.
- Required target display name, static/company legacy fallback and no-profile
  cases need explicit materialization or product-owned inputs. Never retain
  `_legacy_brand` as a second live profile authority after the switch.
- Source primary and secondary may be independently absent. Target
  `validate_brand_values` refuses accent without primary in the submitted
  payload, even when a primary already exists on the row. Define handling for
  partial edits and inherited colors without falsifying source provenance.
- Source deactivation and reactivation need equivalent typed commands. The
  inspected service has activation but no tenant retirement/deactivation
  command; a direct ORM status write would invent another owner.
- Target model/migration uniqueness includes `profile_code`; it does **not**
  enforce the comment's claimed one ACTIVE profile per tenant/scope. The
  resolver iterates all matching rows without ordering. Multiple codes at one
  scope can make output order-dependent, including a null tenant-default ID.
  Require a demonstrated uniqueness/admission contract before backfill/switch.
- Target `resolve` aggregates locks and `resolvable_by` exposes their union;
  this is not a write permission/legal-owner check. No source lock data exists.
  Approve identity-field override permissions separately; do not silently
  introduce new legal authority or choose locks in this packet.
- Source commands have commit wrappers; target upsert/activation use Kernel
  `execute_once` and flush-only handlers. The assembly must define one command
  transaction, stable idempotency identities, audit actor/record version,
  in-flight fencing and rollback. Target tenant commands do not write audit
  events themselves. Existing company-info save commits before and after its
  profile sync; preserve its owner while proving the replacement consequence.

### Asset mapping and actual secret requirements

| Source class / path | Candidate handling and outstanding proof |
|---|---|
| `/branding/assets/<UUID>` | `branding_storage.py` parses a `StoredFile` ID; prove file-owner correlation, tenant/authorization/lifecycle, MIME and byte digest before producing an opaque target reference. `public/branding.py::branding_asset` serves only undeleted `branding_asset` records through the current storage owner. |
| `/static/...` | Product-packaged asset, including generated favicons. Decide explicit retained product reference versus governed file import; prove content and serving behavior. Target does not fetch these paths. |
| HTTP(S) URL | External locator, not byte identity or file reference. Decide authorized file ingestion versus explicitly typed product locator retention; no remote fetch or credentials were used here. |
| Null / empty / removed asset | Preserve inheritance/clear semantics. Source settings handler can delete local files or hard-delete managed objects; prove replacement/delete/reference sharing and failure atomicity before changing that flow. |
| Invoice-only `data:` fallback | `billing_invoice_pdf.py::_logo_src` supports inline data and settings/StoredFile/static fallback, beyond profile-upsert's accepted URL classes. Classify actual use before retiring this presentation path; not an extra accepted profile URL class. |

Brand Profiles stores references and never reads bytes. Its Python dependencies
contain no Files package or provider credential. File capability is an assembly
integration requirement if preserving uploaded/managed assets, not a sibling
package dependency to add to Brand. The existing Sub file-storage/provider
configuration may require object-store credentials at runtime; approve the
selected file graph and credential owner separately. SMTP/sender credentials,
registry access, database-role credentials and mobile signing material are not
brand-profile values. No credential reads, provider calls, byte downloads,
registry census or secret-custody operation occurred in this preparation.

### Candidate artifact graph and proof frontier

The graph below names source-declared edges and lineage requirements, not an
installable lock. Exact package hashes and compatibility must close independently
before this module's switch; unrelated Party/Customers completion is not a hold.

| Node / edge | Pinned evidence and unresolved proof |
|---|---|
| `dotmac-brand-profiles` 0.1.0a1 | `pyproject.toml`: Python >=3.11,<3.14; Kernel >=0.1.0a77, UI >=0.1.0a7, SQLAlchemy ^2.0. `module-release-legacy-unverified.json` names tag object `17e3140170ebcbb9810a67b6b97256cbbf9be6d1`, peeled source `ed69f9dfdeea493dab7d7ba25c04e940f0870545`; historical/non-pinnable. No verified row or wheel hash established. Source inspection at current Starter SHA is not proof of those historical bytes. |
| Kernel candidate 0.1.0a104 | `kernel-release-verifications/0.1.0a104.json`: source `bf16e30efc3321ca9a495de31570bb5f9a02e9e3`, wheel SHA256 `183e0aa003db22fc2cfd8a846df14589027e34a9632180b018222d5eac0d6076`, publish run `35360556616`, verify run `35360926284`. Range satisfaction alone is not exact released-byte module compatibility. Current unpublished `+dev` is not a substitute. |
| UI candidate 0.1.0a8 | `release-record-kernel-a99-ui-a8.md`: source `8c92943062dbfe7f17bfa35264243709ec3d92c3`, run `33361250324`, bundle SHA256 `1997fe0ca07c058056cefc6ef8a01d4355c4c9c95d678e251b4b2ae08b8abd24`. Per-file wheel digest/readback still needed. Assembly maps primary/accent into UI and reviews both rendered pipelines. |
| `brand_profiles` TENANT lineage | `manifest.py`: `short_code=brand`, prefix `bp`, branch `brand_profiles`; `bp_0001_brand_profiles` creates `mod_brand.brand_profiles`. Common prerequisites `module_database_roles.v1`, `idempotency_ledger.v1`; TENANT `tenant_scope_catalog.v1`. Resolve their exact released migration revisions and prove ENABLE/FORCE RLS, tenant function and app-role behavior on hosted disposable PostgreSQL. Exclude platform profile/host-binding tables and platform audit prerequisite from this selection. |
| File integration, if selected | `dotmac-files` current source declares 0.1.0a5, Kernel >=0.1.0a98 and SQLAlchemy ^2.0. These are source coordinates only; no Files wheel/release/proof was inspected or selected. Prove a compatible TENANT file owner/adapter and its migration/runtime provider graph before any opaque ref claims. |
| Other dependencies | Freeze exact Python/SQLAlchemy and transitive versions with the chosen verified artifacts; a broad version range is not a reproducible graph. |

Kernel smoke SHA beginning `545` is recorded for a local smoke recipe that
omits the CSS-generation step; the release recipe associated with SHA beginning
`183e` runs that step before packaging. Recipe tracing does not verify retained
direct bytes, and the two hashes are not
interchangeable identities. Obtain exact released-byte compatibility receipts;
never rebuild an existing tag. No package build/install/download, release/tag,
lineage execution, backfill, shadow, or cutover was performed here.

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

### Bounded reader and writer census at the source SHA

AST import census over tracked Python under `app`, `scripts`, `alembic` finds
17 importers of the profile service, 12 importers of deployment-static
`branding_config`, and four importers of the profile ORM. The sets overlap;
these are path counts, not writer counts or total template reach. Together
with direct symbol/URL/key searches, they give these concrete cutover paths:

| Paths | Observed concern / switch disposition |
|---|---|
| `app/services/brand_profiles.py` | Runtime row writer/selector and legacy resolver: `upsert_brand_profile`, `sync_platform_brand_from_legacy_settings`, both commit wrappers, `deactivate_brand_profile_committed`, `list_brand_profiles`, `resolve_brand`. Replace selected profile owner and fence legacy paths at switch. |
| `app/api/branding.py` | Resolve/list GET, PUT upsert, DELETE deactivate adapters; preserve typed response and clear/lifecycle semantics through new owner. |
| `app/web/admin/system.py`, `app/services/web_system_company_info.py`, `scripts/one_off/backfill_brand_profiles.py` | The other three service importers delegate profile sync; settings/assets/company ownership differs. Retire/repoint profile consequence only, including the script's dry-run flush/rollback path. |
| `app/api/me.py`, `app/web_home.py` | Subscriber-scoped API read and default landing-page read. |
| `app/web/customer/branding.py`, `app/services/web_reseller_routes.py`, `app/services/web_admin.py` | Default/subscriber/reseller resolution; sidebar/auth consumers use these projections. |
| `app/web/public/branding.py` | Default theme CSS and manifest reads; login-hero remains a separate settings concern; asset serving is a separate file concern. |
| `app/services/billing_invoice_pdf.py`, `app/services/sales/quote_documents.py` | Scoped document presentation. Invoice logo can bypass the selected profile through settings/StoredFile/static fallback. Retain financial and legal fact owners. |
| `app/services/notification_adapter.py`, `app/services/support.py`, `app/tasks/notifications.py` | Scoped brand in customer communications; delivery/consent remains outside profile ownership. |
| `app/services/team_inbox_projection.py`, `app/services/web_catalog_subscriptions.py` | Inbox company-name projection and subscriber catalog presentation. |
| `app/services/public_branding.py` | Direct ORM active-favicon lookup plus legacy comms-setting lookup; repoint the profile read and retire displaced profile fallback. |
| `app/services/migration_source_export.py` | Direct ORM read-only source export; retain historical source-artifact meaning and declare post-switch availability/retention. |
| `app/models/__init__.py` | ORM registration import, not an independent reader or writer. |
| `app/web/brand_globals.py`, `app/services/email_template.py` | Static template brand default, with explicit supplied brand able to override the email default. Declare remaining product defaults after switch. |
| `app/services/email.py` | Static from/support/legal fields plus company-info and comms-logo bypasses. Separate presentation from connector authorization; do not transfer SMTP authority. |
| `app/web/public/invoice_checkout.py`, `app/services/web_customer_actions.py`, `app/services/events/handlers/notification.py`, `app/services/ncc_report_email.py`, `app/services/nextcloud_talk_staff.py`, `app/services/staff_notifications.py`, `app/services/ticket_mentions.py`, `app/services/web_system_export_tool.py` | Deployment-static importers using application origin and/or presentation. Keep origin/export/delivery ownership explicit; replacing all `get_brand` calls blindly would move unrelated authority. |
| `app/web/portal_branding.py`, `app/web/reseller/branding.py` | Indirect sidebar/request-state projections; context/cache invalidation and scope parity must be tested. |
| `templates/base.html`, `templates/auth/*.html`, `templates/index.html` | Indirect brand/context consumers and static favicon/PWA/theme defaults; compare actual rendered output and failure defaults. |
| `scripts/utils/settings_sync.py`, `app/services/settings_spec.py` | Generic settings writer and specs for `sidebar_logo_url`, `sidebar_logo_dark_url`, `favicon_url`, primary/secondary and five semantic override keys; classify/gate these branding-key writes at switch, preserve unrelated settings. No script was executed. |
| `alembic/versions/267_brand_profiles.py`, successor `268_sot_safety_closure.py` | Applied schema/seed history and its lineage link. Preserve history; not runtime writer cleanup. |

Search boundary: imports and direct references in this pinned repository, plus
named indirect context/templates and settings/asset bypasses. Dynamic imports,
external scripts, deployed jobs and actual configured data are not proved by
this census; in-flight writer admission, hosted runtime tracing and the final
candidate's scanner/architecture receipt must close that frontier. This does
not schedule pre-cutover refactors or claim all legacy writers are retired.

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
