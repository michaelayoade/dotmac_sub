# Field mobile map read contract

Field mobile renders reference plant supplied by `app.services.field.map_assets`;
native plant/GIS records remain authoritative. This client correction does not
change backend disclosure, asset ownership, location-write policy, or permissions.
The legacy field-map asset service is not yet declared in the executable SOT
registry; registering or migrating that backend owner is separate architecture work.

## Technician reference plant

`GET /api/v1/field/map-assets` takes repeated `asset_type` query values, `limit`
and `offset`. Its response is `ListResponse[FieldMapAsset]`: `items`, `count`,
`limit`, `offset`. Count means rows in this page, not all matching records.
Supported layers are FDH cabinets (`fdh_cabinet`), splice closures,
fiber access points, service buildings and wireless masts. OLTs and `fdh` are not
accepted asset types. All supported layers except service buildings start selected.

The encrypted principal/deployment-scoped cache is a rebuildable projection.
A cold read fetches all pages for each selected type. A cached read returns the
last snapshot immediately and starts a background replacement after five minutes
or when selected layers have no freshness record. Each type is replaced together
with its local completion timestamp in one database transaction, only after every
page succeeds. Invalid coordinates are excluded from pins, but still count when
deciding whether another page exists. Failed background reads retain the prior
snapshot and freshness timestamp. A successful empty snapshot removes old assets.

The API accepts `updated_since` but sends neither deleted/inactive records nor a
server snapshot cursor. Therefore the client must not use incremental merging:
it cannot distinguish a removed asset from an unchanged asset. The local timestamp
means fetch completion, not authoritative server freshness. Offset pagination is
not a stable server snapshot under concurrent plant changes; a later full refresh
repairs drift. No atomic multi-page server snapshot guarantee is claimed.

## Vendor assigned-job map

The vendor map consumes fresh assigned jobs and each job's typed destinations.
Only the work-order site and exact linked outage/project targets may appear;
proximity, company membership and device position never authorize plant access.
The old nearby/search inventory APIs refuse vendors, as do the global field
plant APIs and mutation routes. The map has no retained offline plant fallback.

Navigation uses the existing `JobDestination.mapsUri` and
`uriLauncherProvider`: verified coordinates launch directions; the job's own
address may launch address search. Missing coordinates never select a nearby
asset or parent site. See `VENDOR_JOB_MAP_PRIVACY.md` for the current-assignment
and exact issuance-target gates and the page contract.

## Validation

Focused Flutter tests cover canonical filters, pagination past a full page,
invalid coordinates, cache replacement/deletion, failed-page preservation and
vendor navigation handoff. `tests/test_field_mobile_map_contract.py` pins client
query/layer vocabulary against actual FastAPI/OpenAPI and Pydantic contracts.
Full Flutter analysis and tests remain required before publication.
