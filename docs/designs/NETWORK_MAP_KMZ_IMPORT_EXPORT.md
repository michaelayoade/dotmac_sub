# Network Map KML/KMZ import and KMZ export

Status: implemented for `GET /admin/network/map`.

## Page contract

- **Screen:** `/admin/network/map`, operational canvas.
- **Audience:** network operations staff.
- **Job:** stage externally prepared plant geometry for governed review and
  exchange an authorized view of the current map with KMZ-compatible tools.
- **Read owner:** `ui.network_map_projection`.
- **Transfer owner:** `network.map_kmz_transfer`.
- **Staging owner:** `network.fiber_source_staging`.
- **Canonical plant writers:** the existing fiber identity, connectivity,
  review, and asset-change owners.
- **Primary action:** Import KML or KMZ for principals with `network:fiber:import`.
- **Secondary action:** Export KMZ for principals with `network:map:export`.

Imported rows are observations. They are rendered only in the import result as
a warning-labelled preview overlay and never join the production map
projection. The preview retains placemark names, descriptions, and supported
geometry, including geometry on unclassified and unsupported rows. Unsupported
rows keep only the minimum evidence needed for review; customer/device IDs and
private ExtendedData remain excluded. Canonical plant changes remain
independently reviewed. Coordinate proximity never establishes identity or
connectivity. The transfer owner returns typed per-feature proposal eligibility
for the existing point-asset review workflow; the browser presents that result
and does not infer eligibility from geometry or match state.

## Import contract

The browser accepts one KML or KMZ file per command and uses **Mixed network
map** automatically. Legacy closed profiles remain available to existing
non-browser callers. The mixed profile accepts a heterogeneous batch of
supported fiber plant types: fiber segments, access points, FDH cabinets,
splice closures, service buildings, and support structures. It reads KML
placemark names, IDs, descriptions, styles, `ExtendedData`, and Point,
LineString, and Polygon geometry. `dotmac_asset_type` (or `asset_type`,
`feature_type`, or `type`) and a stable source ID such as `dotmac_asset_id`,
`spanid`, `access_pointid`, `fibermngrid`, `enclosureid`, `buildingid`, or
`poleid` improve classification and matching. IDs are optional for staging.
Each type accepts compatible geometry: fiber segments require lines, support
structures require points, and the other supported plant types accept points
or polygons. Missing or duplicate IDs become review evidence instead of
blockers.

When a type is missing, the parser may offer a suggestion based on line
geometry or explicit placemark wording, but retains the feature as
`unclassified` until an operator confirms its type. A point or polygon with
multiple possible plant types is never classified from geometry alone. An
unclassified row remains blocked from canonical asset review until its type is
resolved.

Mixed imports do not admit customer, ONT, OLT, POP, or network-device features
as fiber assets. Such rows are retained as blocked evidence so the operator
can see the placemark name and geometry that needs a different owning
workflow. Their identifiers and private ExtendedData are removed before
persistence.

The admission boundary:

1. requires an authenticated actor, `network:fiber:import`, a reason, and an
   idempotency key;
2. limits the upload to 25 MB, KMZ archives to 64 entries, the enclosed KML
   document to 100 MB, and the compression ratio to 200:1;
3. accepts a raw KML file or a KMZ containing exactly one KML document and
   parses it with entity-safe XML. Styles and HTTPS icon references are
   retained as metadata only; this service does not resolve DNS or fetch
   external resources, and the preview uses default symbols;
4. validates WGS84 coordinates and each feature's supported type geometry;
5. binds the command key to actor, filename, file digest, profile, and reason;
6. records immutable source and feature digests, normalized geometry, match
   suggestions, blocker codes, actor, audit evidence, and a transactional
   `network_map.kmz_import_staged` event; and
7. returns a typed preview. A mixed batch with no structural blockers is staged
   even when it contains multiple geometry or asset types. Blocked,
   unclassified, or ambiguous rows are never discarded or promoted
   automatically. Unsafe icon schemes and obvious internal icon addresses are
   reported and ignored; they do not block valid feature geometry. KML
   `NetworkLink` documents are never fetched or expanded. Local placemarks in
   the same file remain staged with a warning; link-only files must be downloaded
   and imported directly.

Exact command replay returns the existing batch. Reusing the key for different
inputs fails closed. An identical source/profile manifest also reuses its
immutable staging batch.

The stored staging evidence does not contain the raw archive. It retains the
normalized source facts and both file and manifest hashes required to prove
which bytes were admitted. Retention follows the fiber migration evidence
policy. A repair reruns admission from the checksum-bound source; it never
edits a staged feature in place.

## Export contract

The export adapter accepts typed layer, scope, customer-inclusion, and WGS84
viewport-bound inputs. The service rebuilds the authoritative map projection
for every request and applies those inputs server side. Browser-posted feature
data is never serialized.

The KMZ contains one deterministic `doc.kml`, stable feature ordering, folders
for infrastructure, fiber, network devices, ONTs, and customers, and typed
`ExtendedData` values. Management IPs, serial numbers, notes, signal readings,
credentials, links, and raw telemetry are excluded. Customer placemarks are
included only when the caller also has `customer:read`; they are informational
and are not accepted by the fiber importer.

Visible export uses the active map layers and current viewport. If the page is
opened with `focus=customers` before a layer is selected, the customer layer is
the export default. Export is a read-only query and produces no durable state.

## Transactions and failure behavior

`stage_network_map_kmz` enters `execute_owner_command` once on a
transaction-free session. The source batch, normalized features, audit row,
and outbox event commit together. Parser, validation, idempotency, uniqueness,
audit, or event failure rolls the whole command back. The staging participant
uses `flush()` only.

Malformed files and missing KML return stable domain errors with archive or
parser context. Unsupported feature types, incompatible geometry, out-of-range
coordinates, and incomplete source evidence remain visible with the affected
placemark name, row, reason, and correction guidance. External icons are never
fetched; unavailable or unsafe references use default symbols without dropping
feature geometry. The UI keeps canonical layers usable when preview rendering
fails.

## Schema and rollout

Migration `627_network_map_kmz_transfer` adds idempotency constraints and permissions to existing source batches. Migration `642_network_map_import_feature_classification`, after `641_customer_vacation_pause_resume_at`, adds append-only reviewed type revisions for staged features; it does not modify existing batches. Existing source batches remain valid without a backfill. The classification migration uses a five-second lock budget and thirty-second statement budget. The source-batch table is expected
to be an operator-scale evidence ledger; operators must inspect its row count
and active locks before applying the constraint on an existing deployment.

Rollout is expand-only. Grant permissions to reviewed roles, confirm imports
remain staged, complete independent fiber review before canonical application,
and verify exported KMZ files in a compatible viewer. A complete mixed batch
means every row has a supported fiber plant type, compatible valid
geometry, and no structural blocker; it remains immutable staging evidence and
does not bypass identity, connectivity, or asset-change review. Operators can
record feature type choices as append-only, audited classification revisions;
the original staged rows and source file remain unchanged. The map UI's explicit
submit action creates independent proposals only for eligible new point assets
covered by the existing governed asset-proposal workflow. Matched features,
routes, buildings, and unsupported features remain staged; proposal approval is
a separate action. Classification and proposal submission do not directly apply
route connectivity or modify canonical map rows. Rollback removes only the new
permissions and nullable idempotency columns; it does not delete existing
source batches. If the unique constraint cannot be added within budget, stop
and forward-fix after resolving the lock or unexpected volume; do not widen the
timeouts during an application deployment.
