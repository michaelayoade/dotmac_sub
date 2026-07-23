# Subscriber Service Location — Source of Truth

## Outcome

A subscriber's **service location** — the address text *and* its coordinate —
has one canonical owner and many consumers. The map, field services, billing,
installation, and NCC reporting all read the same record. No surface keeps a
private copy, and no fact is stored in two places that can drift.

The canonical record is the **service `Address`** (`app/models/subscriber.py`,
`addresses` table, `address_type = service`), which carries the address text and
`latitude`/`longitude`/`geom(POINT, 4326)`.

## Canonical owners

| Concern | Owner | Notes |
| --- | --- | --- |
| Service address identity + text | customer/subscriber domain (`customer.accounts`) | Creates and maintains the `Address`; projects verified location facts onto it. |
| Coordinate projection to the map | `gis.spatial_sync` (`app/services/gis_sync.py`) | Writes `latitude`/`longitude`/`geom` and projects `Address` → `geo_locations`. Owns *where*, not *what*. |
| New location capture | `customer.location_capture` (`app/services/location_capture.py`) | Field-arrival GPS, portal pin, agent. Feature-gated. |
| Capture adjudication | `customer.location_verification` (`app/services/geocode_reconciler.py`) | Adjudicates a pin against the claimed location; writes the verification ledger. |

Coordinates and spatial projection stay with `gis.spatial_sync`; the customer
domain owns what the address is. A captured pin flows capture → verification
ledger → the subscriber owner projects it onto the canonical `Address`.

Consumers (OSP/network map, field services, billing, installation, NCC) read the
canonical `Address` or its `geo_locations` projection. `app/services/field/
map_assets.py` currently holds its own coordinate writer + geocoding — a parallel
copy to converge onto the owner.

## The inline columns are legacy — migrate in, then remove

The Splynx cutover left service address as denormalised inline text on
`subscribers` (`address_line1`, `address_line2`, `city`, `region`; the `billing_*`
inline columns are empty). This is a second copy of an owned fact and must not
survive. On staging: of 15,286 subscribers only 6,204 have `address_line1`, 3,589
a city, 11 a region, 0 billing — the inline data is thin; Splynx `customers`
(street_1/city/zip/gps) is the richer source.

**These columns are removed, not demoted** — a demoted read-copy can still drift.
Removal is a phased cutover, because ~20 app files read the inline fields.

## Historical backfill (Splynx → Sub)

The legacy Splynx billing DB (restored to the `splynx_restore` container on
seabone) is the historical source. Runner: `scripts/migration/import_splynx_geo.py`.

- POP/BTS coordinates: Splynx `network_sites` → `pop_sites`, matched by name
  (exact + token-subset), creating POPs for geocoded BTS with no match (via the
  network POP owner `web_network_pop_sites.create_site`).
- Subscriber service address: Splynx `customers` (street_1/city/zip + gps) →
  canonical service `Address`, joined `customers.id = subscribers.splynx_customer_id`,
  with sub inline text as fallback. GPS parsing (range-validated, axis-swap
  repaired) lives in `app/services/splynx_geo_import.py`.
- Coordinate write + projection go through `gis.spatial_sync.apply_pop_coordinates`
  / `apply_address_coordinates`.

Splynx data is dirty and handled: `gps` may be `lat,lng,alt` or axis-swapped;
`city` sometimes holds a numeric login id (nulled); `street_1` may exceed
`address_line1`'s length (truncated).

Staging result (2026-07-23): `pop_sites` 23→31 (28 geocoded); `addresses` 0→6,404
service rows, 1,739 with coordinates; `geo_locations` 1,767 features (1,739
address + 28 pop).

## Migration / cutover plan (explicit authority migration)

1. **Materialise** the canonical `Address` from Splynx(+inline). *(done, staging)*
2. **Repoint consumers**: the ~20 files reading `subscribers.address_line1/
   city/region` read the canonical `Address` instead (a single resolver/accessor
   returns the subscriber's service `Address`, falling back to inline only during
   the transition window).
3. **Verify parity**: every subscriber that presented an inline address still
   presents one via `Address`; no consumer regresses. Focused tests.
4. **Drop the inline columns** (`address_line1`, `address_line2`, `city`,
   `region`, and the empty `billing_*`) in a quiesced migration — the last step.
   Dropping before step 2 breaks the consumers.

## NCC location capture

The NCC quarterly Subscriber return aggregates subscribers by **state** and, where
required, **LGA**. `ncc_subscriber_report.infer_state` resolves each subscriber's
state in priority order: `subscriber.region`/`billing_region` → `Address.region` →
city → address text. Once the inline `subscriber.region`/`city`/`address_line1`
are removed, the canonical `Address` becomes the primary NCC location source.

- **The `Address` schema is already NCC-complete** — `AddressBase`/`AddressCreate`/
  `AddressUpdate` carry `region` (state), `lga` (NCC Local Government Area), `city`,
  `postal_code`, and coordinates. No schema change is required.
- **Authoritative capture** of state + LGA is by reverse-geocoding the verified pin
  (`geocode_reconciler.reverse` → NCC state + county→LGA, validated via
  `ncc_location.canonical_lga`), projected onto `Address.region`/`Address.lga`
  (customer-domain projection of the verified fact; the ledger records the
  verification, the Address holds the captured value).
- **Backfill**: the 6,404 materialized addresses carry street text + 1,739 coords,
  but `region`/`lga` are largely unpopulated (Splynx has no state/LGA column).
  Populate region+LGA on the coord-bearing addresses by reverse-geocode; text-only
  rows fall back to report-time inference from `Address` text/city (as today).
- **Removal cutover**: `infer_state` and `ncc_complaints_report` drop the inline
  `subscriber.region/city/address_line1/2` reads and rely on `Address.region/city/
  address_line1/2` (already read as fallbacks). `billing_region/billing_city/
  billing_address_*` STAY on `Subscriber` and are read unchanged.

## Tests

- `tests/test_splynx_geo_import.py` — GPS parse/swap-repair, name matching,
  idempotent coordinate writes + projection.
- Parity + boundary tests added with step 2/3.

## Related

- `docs/SOT_RELATIONSHIP_MAP.md` (geospatial domain #27; customer_context).
- `docs/designs/FIBER_TOPOLOGY_SOT.md` (coordinates owned by `gis.spatial_sync`).
- Knowledge: `sub-subscriber-location-inline-no-coords`,
  `splynx-seabone-restore-geo-source`, `crm-parallel-osp-fiber-authority`.
