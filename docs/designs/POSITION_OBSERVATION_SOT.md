# Position observation source of truth

Status: product-first source cut over; shared-module extraction pending

## Ownership

`operations.position_observations` is Sub's sole writer for field-technician
location evidence, collection state, and the rebuildable current-position
projection. The current product implementation is the qualifying source for
Starter ADR-0032 and the future `dotmac-positioning` module.

It does not own technicians, shifts, work orders, dispatch, routes, attendance,
customer disclosure, ETA, or map presentation. A location observation can
produce a durable provider-neutral fact, but only a separately registered Sub
policy/event owner may decide a work-order consequence. Ingest never calls the
field transition engine.

`operations.field_presence` separately owns Sub's workforce status vocabulary
and current shift/presence state. A position ping cannot carry or mutate that
status. During the pre-module migration the two owners write disjoint columns
of the legacy `field_tech_presence` row; module cutover physically separates
the positioning projection from the product presence record.

## Write contract

The API converts the authenticated principal and validated Pydantic payload to
`RecordLocationBatchCommand`. Each observation carries:

- `client_observation_id`, assigned on the device before buffering;
- device `captured_at` and server `received_at`;
- latitude, longitude, and device-reported `accuracy_m`;
- provider-neutral `source`;
- optional opaque `context_ref`, which the Sub adapter maps from its
  `work_order_id` API field; and
- a typed `PositionObservationPolicy` resolved by the product adapter.

Collection starts through a server-issued, purpose-bound lease whose grant and
expiry are stored on the current tracking row. The reusable owner accepts the
purpose as typed command input and knows no product vocabulary; Sub binds its
stable `field_operations` purpose at the adapter. Ingest fails closed when the
matching lease is missing or expired. Disabling collection clears the lease.
The maximum lease comes from the database-authoritative
`field.location_collection_lease_minutes` setting.

The owner enters `execute_owner_command` once on a transaction-free session.
It validates an item before adding any row. One invalid item therefore cannot
be flushed by a later item in the batch. The observation row, current-position
projection, collection state, and opaque outbox evidence commit atomically.
Helpers neither commit nor roll back.

The identity key is `(technician_id, source, client_observation_id)`. The owner
stores a canonical SHA-256 payload fingerprint. An exact retry returns
`replayed`; the same identity with different evidence returns
`position_observation_identity_collision` and never replaces the first fact.

## Evidence quality and projection

Coordinates retain universal geographic bounds. Product policy supplies the
maximum batch size, acceptable accuracy radius, and future clock skew through
`field.location_max_batch_size`,
`field.location_max_accuracy_meters`, and
`field.location_max_future_skew_seconds`; these limits are not fixed in the
positioning owner or request schema. An older accepted observation remains in
the retained audit but cannot move the current projection backwards. For equal
capture timestamps, only a more accurate fix may advance the projection.

The outbox event contains opaque observation and technician identifiers, not
coordinates. An authorized resolver loads the retained observation by id so
the event store does not become a second, unpruned location history.

Customer disclosure is a Sub policy, not a positioning decision. The self-care
reader requires all of: the subscriber-owned active visit, its active assigned
technician, that technician's active collection lease, and a fix inside the
database-authoritative `field.location_customer_stale_seconds` window. A stale
or expired fix returns no coordinates.

The mandatory retention task prunes observations by server `received_at` using
`field.location_ping_retention_hours`; its sweep cadence is configured by
`field.location_retention_sweep_interval_seconds`. The task enters the same
registered owner boundary and emits only deletion counts, never coordinates.

## Migration and compatibility

Migration 542 renames the product context column from
`crm_work_order_id` to `work_order_id`, converts `break` to the canonical
`on_break` status, backfills legacy observation identities/fingerprints, and
adds the unique replay key. The API accepts `crm_work_order_id` only as an
explicit input alias during cutover and emits the canonical field.

Committed observation events are consumed by
`operations.field_geofence_policy`. The event carries only the observation id;
the policy reloads the retained row, ignores evidence that is no longer the
current projection or is outside `field.location_geofence_stale_seconds`, and
asks the existing `operations.field_completion` owner to apply the deterministic
transition. Event redelivery is harmless because that transition uses the same
stable geofence client-event identity.

With the source-hardening canaries in place, Starter may now extract this
behavior and its parity tests. Sub then shadows and cuts over to the released
module before the local writer is retired.
