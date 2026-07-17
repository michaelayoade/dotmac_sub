# Fiber Topology Source of Truth

Status: phase 6 reviewed access attachments and validated trace shadow implemented
Owner: `network.fiber_topology`
Mutation owner: `network.fiber_asset_changes`
Identity-decision owner: `network.fiber_identity_decisions`
Review/reconciliation owner: `network.fiber_identity_review`
Connectivity-decision owner: `network.fiber_connectivity_decisions`
Access-attachment owner: `network.fiber_access_attachments`
Production system: Sub at `selfcare.dotmac.io`

## Outcome

Sub owns the operational path from OLT to customer. Map files, OLT pollers,
field observations, CRM construction routes, and monitoring systems provide
facts. They do not independently decide fiber asset identity, connectivity, or
customer impact.

The target trace is:

```text
POP -> OLT -> PON port -> termination/segment chain -> FDH/FAT/splitter
    -> splitter output/drop -> ONT -> subscription -> customer
```

Only validated edges participate in outage impact or customer diagnosis. A
line that merely appears to touch a point on a map is not a connectivity edge.

The implemented trace begins at the serving POP resolved by `network.identity`.
It does not label LLDP adjacency as a border/core/NAS forwarding path. That
upstream lane remains explicitly outside the trace until an authoritative
routing/access-path cohort projection can prove it. This preserves the target
without inventing the part of the path Sub cannot yet verify.

## Canonical edges

| Edge | Canonical record | Legacy/observed alternatives |
| --- | --- | --- |
| POP to OLT | active `NetworkDevice` OLT match plus `pop_site_id` | names and map proximity are matching evidence only |
| OLT to PON | `PonPort.olt_id` | board/port labels are observed identifiers |
| PON to ONT | `OntUnit.pon_port_id` | `OntAssignment.pon_port_id` is a projection that must agree |
| ONT to service | active `OntAssignment.subscription_id` | subscriber/address fallback is shadow-only until backfilled |
| PON to splitter input | active `PonPortSplitterLink` to an input port | inferred PON/splitter proximity is evidence only |
| Splitter output to ONT | `OntUnit.splitter_port_id` to an output port | `SplitterPortAssignment` is legacy matching evidence |
| Asset-to-asset cable path | `FiberTerminationPoint` plus `FiberSegment` endpoints | route geometry alone is not connectivity |
| Strand/splice detail | `FiberStrand` and `FiberSplice` nested inside the asset graph | strand endpoint fields do not create a parallel asset graph |
| Customer fault verdict | `network.connection_health` using access path and outage impact | UI map state and raw telemetry do not decide the verdict |
| Fiber fault candidate ranking | `network.fiber_topology.localize_fiber_fault` over validated trace assets and fresh OLT cohorts | a highlighted map asset is not a confirmed failure or incident |

Coordinates and spatial projections remain owned by `gis.spatial_sync`. Fiber
topology owns what an asset is and how it connects; GIS owns where its approved
spatial projection is stored.

## Production audit — 2026-07-17

The audit was aggregate and read-only against Sub production. No customer
identifiers or secrets were extracted.

### Electronic path

- 4,054 active subscriptions are fiber.
- 1,504 active ONTs exist; 1,457 have a PON and 1,443 have an OLT.
- 1,412 active ONT assignments exist.
- 1,284 active subscriptions have an exact `subscription_id` assignment; 1,310
  resolve only when subscriber fallback is allowed.
- 39 subscriptions/subscribers have multiple active ONT assignments.
- 14 active ONTs reference a PON owned by a different OLT.
- 19 active assignment PONs disagree with the ONT PON; 10 point outside the
  ONT's OLT.
- One active assignment references an inactive ONT.
- 7 active OLTs exist; 5 are mapped through a monitoring node to a POP.
- 685,289 signal observations cover all 1,504 active ONTs. This is sufficient
  for PON/co-failure clustering, but does not identify an unnamed passive asset.

### Passive plant and locations

- Zero FDH cabinets, splitters, splitter ports, PON/splitter links, FAT/access
  points, splice closures, trays, splices, strands, terminations, and fiber
  segments are loaded.
- All 36 active POPs lack coordinates.
- No active ONT has GPS or splitter linkage.
- No active ONT assignment has a service address.

Sub can currently localize a correlated failure to an OLT/PON/ONT cohort. It
cannot truthfully name a cabinet, FAT, closure, strand, segment, or drop as the
likely fault area.

## Map-source classification

Checked-in KMZ files are source evidence, not production truth:

| Source | Rows | Identity quality | Treatment |
| --- | ---: | --- | --- |
| OSP Paths | 1,600 | 1,600 unique `spanid` values | primary staged cable geometry |
| OSP Access point | 286 | 286 unique `access_pointid` values | primary staged FAT/FAP identity |
| OSP Cabinet | 113 | 113 unique `fibermngrid` values | primary staged cabinet identity |
| OSP Splice info | 1,021 | 1,021 unique `enclosureid` values | primary staged closure identity |
| OSP Building | 1,146 | 1,146 unique `buildingid` values | primary staged building evidence |
| OSP Air fiber | 515 | 515 unique `poleid` values | primary staged support/pole evidence |
| My Map | 19,897 | 4,843 unique names; 15,054 duplicate-name rows | corroborating evidence only |
| My Places Updated map | 16,671 | 2,168 unique names; 14,503 duplicate-name rows | corroborating evidence only |

The CRM vendor-route dataset is construction/commercial evidence. Proposed
routes must not become as-built operational topology without an approved
as-built record. As of this audit, none of those CRM route records or the KMZ
plant records are loaded into Sub production.

## Import and cleanup gates

1. Freeze direct import. The legacy KMZ command is preview-only; whole-table
   purge and direct commit are retired.
2. Stage every source row with source name, stable external ID, content hash,
   raw properties, normalized geometry, and duplicate/match candidates.
3. Produce an operator-reviewed plan: create, update, merge, reject, or hold.
   Name-only and geometry-nearness matches never auto-merge.
4. Repair electronic blockers first: exact subscription assignments, duplicate
   active assignments, ONT/PON/OLT disagreement, OLT-to-POP coverage.
5. Apply approved point-asset identities before cable edges. Preserve source
   provenance and every rejected/merged identity decision.
6. Build terminations and segments. Every operational segment requires two
   referenced termination points and approved geometry.
7. Attach PON input and customer output/drop edges. Splitter port direction
   must be valid and the PON/ONT OLT must agree.
8. Run customer traces in shadow and compare predicted cohorts with OLT signal
   co-failure clusters and field evidence.
9. Cut outage/field-service readers over only when integrity blockers are zero,
   an exhaustive trace-cohort audit meets the approved threshold, and sampled
   paths are field-verified. Aggregate inventory counts alone cannot open this
   gate.
10. Retire subscriber/address fallbacks, duplicate assignment PON state,
    `SplitterPortAssignment` customer authority, and remaining direct CRUD
    writers after their consumers use the owner.

Normalized feature equivalence is tracked by
`(source_system, asset_type, external_id, content_hash)`, while replay of an
identical source cohort is idempotent by its manifest hash. Re-running a source
cannot delete assets absent from that source. Retirement is an explicit reviewed
action, not an import side effect.

## Implemented staging boundary

Migration `333_fiber_topology_staging` adds two evidence tables:

- `fiber_topology_source_batches` is the immutable source manifest. Identity is
  `(source_system, profile, manifest_sha256)` and it records the raw file hash,
  row counts, blockers, candidates, actor, and normalization metadata. Archive
  metadata or feature ordering cannot create a duplicate normalized batch.
- `fiber_topology_staged_features` stores each normalized source fact, stable
  external ID, source properties, GeoJSON, content/geometry hashes, lineage to a
  prior source fact, and a non-authoritative match suggestion.

`network.fiber_source_staging` is the sole writer. Its match states are:

- `new`: no prior or canonical candidate;
- `unchanged`: the same source identity has identical normalized content;
- `exact_external`: one canonical asset has the same stable external code;
- `candidate`: content changed, a normalized-name candidate exists, or the
  source has a possible duplicate name/geometry;
- `ambiguous`: more than one canonical candidate exists;
- `blocked`: the source row lacks stable identity, valid expected geometry, or
  valid Nigerian coordinates, or duplicates an external ID inside the batch.

These states are review evidence, not asset decisions. The staging service only
constructs `FiberTopologySourceBatch` and `FiberTopologyStagedFeature` rows.
Architecture tests prevent the staging module from constructing canonical asset
models or deleting rows.

Operator command:

```bash
# Read-only preview of all stable-ID OSP layers
python scripts/network/stage_fiber_topology_kmz.py --all-checked-in

# Persist source evidence only; still no canonical asset writes
python scripts/network/stage_fiber_topology_kmz.py \
  --all-checked-in --stage --actor "operator identity"
```

The checked-in six-source preview resolves all expected 4,681 rows with stable
IDs and zero structural/coordinate blockers. Duplicate names and geometries are
retained as review candidates rather than silently merged.

## Reviewed point-asset identity boundary

Migration `334_fiber_topology_identity_decisions` adds the reviewed boundary
between staged evidence and canonical topology:

- `fiber_topology_identity_decisions` binds each `create`, `link_existing`, or
  `reject` proposal to the staged feature ID and exact content hash. Only one
  proposal can be active for a feature. The proposer and reviewer must be
  different actors; a declined proposal remains as evidence and a corrected
  proposal can be submitted without deleting history.
- `fiber_topology_asset_source_links` is the canonical source-identity link.
  Its source key is `(source_system, source_asset_type, external_id)`, so one
  imported identity cannot silently point at multiple assets.
- `create` is enabled only for cabinets, FAT/FAP access points, and splice
  closures. Execution creates a pending `network.fiber_asset_changes` request;
  it does not construct a canonical asset. The source link is finalized only
  after that request is independently approved and has an exact asset ID.
- `link_existing` is enabled for those point types and service buildings. It
  writes identity provenance only and does not mutate the linked asset.
- support structures can only be rejected in this slice because Sub has no
  deployed canonical support-structure table. Cable paths remain ineligible
  until reviewed termination connectivity is modeled; geometry is not an edge.

Every transition records actor, reason/review evidence, timestamps, the staged
content hash, and (for creates) the exact resulting fiber change request. A
changed staged feature cannot be approved, executed, or linked under an old
decision.

Operator flow:

```bash
python scripts/network/review_fiber_topology_identity.py propose \
  --feature-id FEATURE_UUID --action create \
  --actor "proposer identity" --reason "review evidence"

python scripts/network/review_fiber_topology_identity.py approve \
  --decision-id DECISION_UUID --actor "independent reviewer" \
  --notes "identity and geometry verified"

# A reviewer can decline instead; the decision remains as audit evidence.
python scripts/network/review_fiber_topology_identity.py decline \
  --decision-id DECISION_UUID --actor "independent reviewer" \
  --notes "identity evidence does not support this action"

# Emits a pending fiber change request; it does not approve the asset mutation.
python scripts/network/review_fiber_topology_identity.py execute \
  --decision-id DECISION_UUID --actor "executor identity"

# Run after the fiber change request reaches applied or rejected.
python scripts/network/review_fiber_topology_identity.py finalize \
  --decision-id DECISION_UUID --actor "finalizer identity"
```

## Operator-scale review and reconciliation

Migration `335_fiber_identity_review_batches` adds immutable proposal batch
manifests and links every batch decision to its exact row. The review owner
provides four operational guarantees:

- the queue selects only the latest staged version of each source identity and
  shows match evidence, eligible actions, active/terminal decision state,
  canonical candidates, source-to-candidate distance, and source-link drift;
- only one active decision may exist for a source identity across all staged
  versions, and changed newer content blocks approval or execution of an older
  proposal;
- preview normalizes and validates every proposal row without writing;
- persistence commits the manifest and all delegated identity proposals in one
  transaction, or writes nothing when any row is blocked;
- a request hash makes exact replays return the original batch even after its
  decisions progress, while the resolved manifest hash binds every feature
  content hash and decision digest.

The finalization sweep is an idempotent reconciler. It observes pending,
applied, or rejected fiber change requests and delegates each projection to the
identity owner. It never approves an asset mutation. If source content changes
after a change request was emitted, an already-applied asset still receives the
exact old source provenance; the latest-source queue then exposes the content
drift for a separate reviewed update instead of orphaning the created asset.

Example batch file:

```json
{
  "reason": "Reviewed Abuja point-asset cleanup cohort",
  "items": [
    {"staged_feature_id": "FEATURE_UUID", "action": "create"},
    {
      "staged_feature_id": "FEATURE_UUID",
      "action": "link_existing",
      "target_asset_id": "CANONICAL_ASSET_UUID"
    }
  ]
}
```

Operator commands:

```bash
# Latest actionable identities with canonical candidate comparison
python scripts/network/review_fiber_topology_identity.py queue \
  --profile osp_cabinets --state actionable

# Read-only validation and stable hashes
python scripts/network/review_fiber_topology_identity.py preview-batch \
  --manifest cabinet-review.json --actor "proposer identity"

# Atomic manifest plus proposal persistence; no canonical writes
python scripts/network/review_fiber_topology_identity.py propose-batch \
  --manifest cabinet-review.json --actor "proposer identity"

# Repair source links from independently reviewed change-request outcomes
python scripts/network/review_fiber_topology_identity.py reconcile \
  --actor "reconciler identity" --limit 100
```

## Independently attested batch execution

Migration `336_fiber_identity_batch_control` makes the operator-scale review
and execution controls durable:

- `fiber_topology_identity_batch_reviews` stores one independent `approve` or
  `decline` attestation for a proposal batch. The attestation copies the batch
  manifest hash, proposer, reviewer, notes, and item count into a stable digest.
  The caller must supply the exact expected manifest hash, and the proposer
  cannot be the reviewer.
- Review is all-or-nothing. Every decision must still be `proposed`; approval
  also revalidates that every source version and link target is current before
  any decision changes state. A mixed or stale batch writes neither partial
  decisions nor an attestation. An exact replay returns the existing evidence.
- `fiber_topology_identity_execution_runs` records each bounded execution with
  the attestation and manifest hashes, actor, limit, decision-level outcomes,
  remaining approved count, immutable result payload, and result digest.
- One execution advances at most 100 already-approved decisions. Each decision
  uses a savepoint so an invalid/stale item remains approved and is recorded as
  an error without discarding valid outcomes from the same bounded run.
- A `create` outcome is only `change_requested`: it emits the exact pending
  `network.fiber_asset_changes` request. Batch execution never approves that
  canonical mutation. Link and reject outcomes remain owned transitions in
  `network.fiber_identity_decisions`.

Operator flow:

```bash
# Inspect the exact manifest and all review/execution evidence.
python scripts/network/review_fiber_topology_identity.py inspect-batch \
  --batch-id BATCH_UUID

# Independent all-or-nothing review of the exact displayed hash.
python scripts/network/review_fiber_topology_identity.py attest-batch \
  --batch-id BATCH_UUID --expected-manifest-sha256 MANIFEST_SHA256 \
  --action approve --actor "independent reviewer" \
  --notes "source identities and actions verified"

# Advance no more than 50 approved decisions and record every outcome.
python scripts/network/review_fiber_topology_identity.py execute-batch \
  --batch-id BATCH_UUID --expected-manifest-sha256 MANIFEST_SHA256 \
  --actor "executor identity" --limit 50
```

The `network.fiber_identity_review` service owns these batch-control records
and delegates decision transitions to `network.fiber_identity_decisions`. It is
not a parallel writer for canonical point assets or source links.

## Reviewed termination and segment connectivity

Migration `337_fiber_topology_connectivity_decisions` adds the first reviewed
path-to-edge lifecycle. `network.fiber_connectivity_decisions` owns the
decision; `network.fiber_asset_changes` remains the only service that applies
canonical termination and segment mutations.

- A proposal is bound to one staged `fiber_segment`, its stable external ID,
  exact content hash, explicit action, actor/reason, segment attributes, and two
  typed canonical endpoint references. Supported endpoints in this slice are
  PON ports, splitter ports, FDH cabinets, FAT/access points, splice closures,
  and ONTs.
- Geometry is retained as exact route evidence. It never chooses, snaps, or
  infers an endpoint. The proposer supplies both canonical endpoint identities,
  and a different actor must review them while the staged source and endpoint
  assets are still current.
- `fiber_topology_termination_resolutions` gives each typed endpoint reference
  one shared termination resolution. Existing active canonical terminations are
  reused; otherwise execution emits one pending termination change request.
  Paths sharing an endpoint reuse the same pending/applied resolution instead
  of creating parallel endpoint truth.
- Only after both termination requests are applied does reconciliation emit the
  pending segment request with the two exact termination IDs and the reviewed
  source geometry. Neither execution nor reconciliation approves any request.
- Applied segments receive immutable source provenance in
  `fiber_topology_segment_source_links`. A rejected termination or segment
  request closes the decision with preserved evidence. `link_existing` can bind
  a staged path to an already-operational segment only when it has two active
  referenced terminations and route geometry.
- Active termination identity is unique by `(endpoint_type, ref_id)`. The
  `fiber_access_point` endpoint type is explicit; FATs are not hidden under the
  ambiguous legacy `terminal` type.

Operator flow:

```bash
python scripts/network/review_fiber_topology_connectivity.py propose \
  --feature-id PATH_FEATURE_UUID --action create \
  --start-endpoint-type fdh --start-endpoint-ref-id CABINET_UUID \
  --end-endpoint-type fiber_access_point --end-endpoint-ref-id FAT_UUID \
  --segment-type distribution --actor "proposer identity" \
  --reason "field-verified endpoint evidence"

python scripts/network/review_fiber_topology_connectivity.py approve \
  --decision-id DECISION_UUID --actor "independent reviewer" \
  --notes "endpoint identity and route evidence verified"

# Emits missing termination requests, or a segment request when both exist.
python scripts/network/review_fiber_topology_connectivity.py execute \
  --decision-id DECISION_UUID --actor "executor identity"

# Advance applied/rejected termination and segment request outcomes.
python scripts/network/review_fiber_topology_connectivity.py reconcile \
  --actor "reconciler identity" --limit 100
```

This slice does not infer path endpoints from spatial proximity and does not
bulk-load staged OSP paths. Operator-scale connectivity batching and production
cutover remain separate gates.

The six direct API mutation adapters for termination points and segments now
return `410 Gone`; their read endpoints remain available. This removes the
highest-risk unreviewed parallel writer. Canonical mutations are applied by
`network.fiber_asset_changes`, while staged path endpoint decisions and their
provenance remain owned by `network.fiber_connectivity_decisions`.

## Reviewed PON-input and ONT-output attachments

Migration `338_fiber_access_attachment_decisions` makes
`network.fiber_access_attachments` the one writer for the active
PON-to-splitter input edge and the ONT-to-splitter output edge.

- `pon_input` decisions bind one exact `PonPort`, its OLT, the current active
  `PonPortSplitterLink`, and an explicit active splitter input port. One input
  can serve only one active PON.
- `ont_output` decisions bind one exact active `OntUnit`, its authoritative PON
  and OLT, the current output, and an explicit active splitter output. The PON
  must already have a reviewed active input on the same splitter, and one
  output can serve only one active ONT.
- `OntUnit.splitter_port_id` is canonical. `OntUnit.splitter_id` is updated in
  the same transaction as its denormalized parent projection. A detach clears
  both. `SplitterPortAssignment` remains legacy matching evidence and cannot
  satisfy the attachment contract.
- Preview performs no writes. Proposal binds the exact before-state and actor
  evidence. A different actor must approve it. Execution locks and revalidates
  every bound PON/OLT/port/splitter value, then records the exact resulting IDs
  and a result digest. Stale inputs close without mutation.
- Geometry, coordinates, names, proximity, and route touching are not accepted
  as attachment evidence. They can guide field verification only.
- Generic PON-link create/update/delete adapters now return `410 Gone` at both
  API and service boundaries. Generic ONT create/update cannot set attachment
  fields, and the ONT location form no longer writes those fields.

Operator flow:

```bash
python scripts/network/review_fiber_access_attachments.py preview \
  --attachment-type pon_input --action attach --subject-id PON_UUID \
  --splitter-port-id INPUT_PORT_UUID

python scripts/network/review_fiber_access_attachments.py propose \
  --attachment-type ont_output --action attach --subject-id ONT_UUID \
  --splitter-port-id OUTPUT_PORT_UUID --actor "proposer identity" \
  --reason "field labels and electronic PON identity verified"

python scripts/network/review_fiber_access_attachments.py approve \
  --decision-id DECISION_UUID --actor "independent reviewer" \
  --notes "exact PON, OLT, splitter, and output verified"

python scripts/network/review_fiber_access_attachments.py execute \
  --decision-id DECISION_UUID --actor "executor identity"
```

This slice establishes the reviewed writer and invariants. It does not import
production attachments or infer them from the map. Existing exact ONT
assignment duplicates and ONT/PON/OLT disagreements remain a separate repair
slice before customer-trace cutover.

## Validated subscription trace shadow

`network.fiber_topology.trace_fiber_subscription` resolves one ordered path
using only authoritative edges:

```text
serving POP -> OLT -> PON -> referenced terminations/segments
  -> FDH/FAT -> splitter input/output -> referenced drop segments
  -> ONT -> exact subscription -> customer
```

The resolver has no subscriber, address, name, proximity, or geometry-touch
fallback. It requires exactly one active `OntAssignment.subscription_id`,
agreement between the ONT/PON/OLT records, directed splitter ports, active
referenced termination points, and active segment edges with approved geometry.
If two equally short explicit paths exist, the resolver reports an ambiguity
for manual review instead of choosing one.

The read-only admin surface is `/admin/network/fiber-trace`. Operators can find
a fiber service by customer, account, ONT serial, or subscription UUID. It shows
the validated chain, every explicit gap, telemetry freshness, shared-cohort
counts, and ranked candidate scopes. It has no topology write or outage command.

### Fiber service trace page contract

- **Screen and audience:** read-only NOC/support investigation page for tracing
  one active fiber service and deciding the next evidence-gathering step.
- **Primary entity and identifiers:** subscription/customer, account number,
  subscription UUID, offer, and ONT serial. These are internal operational data
  and remain permission-scoped by `network:fiber:read`.
- **Read owner:** `network.fiber_topology` owns the ordered validated trace,
  explicit gaps, telemetry freshness classification, and bounded fault-candidate
  ranking. The route and template do not infer topology or outage state.
- **First viewport:** search, customer/service identity, electronic and physical
  completeness, current OLT observation state, and the evidence boundary.
- **Actions:** the primary action is searching/selecting a service to trace.
  Trace links are read-only row actions; the outage-impact link is secondary.
  There are no bulk, destructive, topology-write, or outage-declaration actions,
  so no command or action-eligibility owner applies.
- **Worklist:** customer/account, offer, ONT serial/state, observation time, and
  trace link; server-side search is bounded to 20 rows (maximum 50), defaulting
  to active fiber subscriptions ordered by observed ONT state and freshness.
  There is no pagination, total, or export in this shadow surface.
- **State and provenance:** incomplete topology, stale/unavailable telemetry,
  online/offline observations, no admissible candidate, and no matching service
  stay distinct. Every gap and candidate includes backend-owned evidence or
  rationale; observations never mutate topology.
- **Depth and projection:** identity and completeness are glance depth; the
  ordered chain and ranked cohorts are investigation depth. Raw asset evidence
  is progressively disclosed in the chain. Desktop may show the chain
  horizontally; mobile keeps identity/state first and makes the chain and table
  horizontally scrollable without dropping semantics.
- **Audit and observability:** the page is read-only. Reviewed identity and
  connectivity decisions retain their own immutable provenance and audit
  records; this projection creates no official incident or outage timeline.

The aggregate audit and the cutover gate are deliberately separate:

- `aggregate_preconditions_ready` means inventory-level blockers are clear;
- `customer_trace_cutover_ready` additionally requires an exhaustive cohort
  evaluation in which every active fiber subscription has a complete trace;
- an omitted or limited cohort fails closed, even if every sampled path passes.

Run the exhaustive gate only as an operator audit because it resolves every
active fiber service:

```bash
python scripts/network/audit_fiber_topology.py --verify-customer-traces

# Shadow sample only; this can never report cutover-ready
python scripts/network/audit_fiber_topology.py \
  --verify-customer-traces --trace-limit 100
```

The local migration chain now introduces a canonical `fiber_access_point`
termination type and a unique active typed-endpoint resolution. A staged FAP/FAT
still appears in a customer trace only after the reviewed connectivity workflow
has projected its canonical termination and segment edges. The resolver never
promotes a staged point or a route touching that point into an operational hop.

## Fault localization

Fault localization is derived, never written by a map UI:

- OLT or monitoring-node outage: all downstream validated paths are candidates.
- PON co-failure: paths sharing the PON are candidates.
- Correlated optical degradation within a validated passive branch: shared
  upstream segment/closure/FAT assets are ranked candidates.
- One ONT down while peers are healthy: rank the customer drop, splitter output,
  ONT power/device, and premises path; do not declare a feeder fault.
- An incomplete trace returns the first explicit gap and the last validated
  scope. It never invents a passive asset from proximity.

The implemented ranking is bounded to one selected service and its exact active
fiber cohort on the same OLT. Stale observations are excluded. When a PON cohort
is jointly offline, the result names the PON plus its validated shared branch as
a candidate set; it does not choose an individual segment without field or
optical evidence. When peers are healthy, the customer drop/premises/ONT scope
is ranked instead. `network.outage_impact` and the outage command owner remain
the only paths that may turn validated infrastructure state into operational
consequences.

The field-service surface consumes the same trace and records observations or
reviewed change requests. It does not directly redefine topology.
