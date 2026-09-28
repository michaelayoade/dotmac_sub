# Network Map KMZ import and export

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
- **Primary action:** Import KMZ for principals with `network:fiber:import`.
- **Secondary action:** Export KMZ for principals with `network:map:export`.

Imported rows are observations. They are rendered only in the import result as
a warning-labelled preview overlay and never join the production map
projection. Canonical plant changes remain independently reviewed. Coordinate
proximity never establishes identity or connectivity.

## Import contract

The browser accepts one KMZ file per command. The selected closed profile
determines the expected asset type, stable external identifier field, and
geometry type. The initial profiles cover OSP paths, access points, cabinets,
splice information, buildings, and air-fiber support structures.

The admission boundary:

1. requires an authenticated actor, `network:fiber:import`, a reason, and an
   idempotency key;
2. limits the compressed upload to 25 MB, the archive to 64 entries, the KML
   document to 100 MB, and the compression ratio to 200:1;
3. accepts exactly one KML document and parses it with entity-safe XML;
4. validates WGS84 coordinates, the profile geometry, and stable source IDs;
5. binds the command key to actor, filename, file digest, profile, and reason;
6. records immutable source and feature digests, normalized geometry, match
   suggestions, blocker codes, actor, audit evidence, and a transactional
   `network_map.kmz_import_staged` event; and
7. returns a typed preview. Blocked or ambiguous rows are never discarded or
   promoted automatically.

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

Malformed archives, missing KML, unsupported geometry, missing external IDs,
out-of-range coordinates, and profile mismatches return stable domain errors
or blocker evidence. The UI keeps canonical layers usable when preview
rendering fails.

## Schema and rollout

Migration `626_network_map_kmz_transfer` adds nullable command-key and command-
fingerprint hashes to existing source batches, their length checks and unique
key, plus separately assignable import and export permissions. Existing source
batches remain valid without a backfill. The migration uses a five-second lock
budget and thirty-second statement budget. The source-batch table is expected
to be an operator-scale evidence ledger; operators must inspect its row count
and active locks before applying the constraint on an existing deployment.

Rollout is expand-only. Grant permissions to reviewed roles, confirm imports
remain staged, complete independent fiber review before canonical application,
and verify exported KMZ files in a compatible viewer. Rollback removes only the
new permissions and nullable idempotency columns; it does not delete existing
source batches. If the unique constraint cannot be added within budget, stop
and forward-fix after resolving the lock or unexpected volume; do not widen the
timeouts during an application deployment.
