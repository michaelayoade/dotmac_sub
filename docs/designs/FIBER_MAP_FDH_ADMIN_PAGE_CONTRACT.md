# Fiber Map and FDH Admin Page Contract

Status: adopted for the fiber-map and FDH cabinet operational surfaces.

## Fiber plant map

- Screen: `/admin/network/fiber-map`; audience: authorized network operators.
- Job: inspect mapped fiber plant, plan service routes, and submit reviewed
  proposals for supported point-asset movements.
- Read owner: `ui.network_map_projection` and the existing fiber-plant map
  projection for the features each layer displays.
- Mutation coordinator: `network.map_asset_change_governance`; canonical asset
  writer: `network.fiber_asset_changes`.
- Dragging a supported FDH or closure submits a movement proposal. The map
  leaves the marker at its approved location until a reviewer approves the
  proposal. Errors show a safe owner message and leave the marker unchanged.
- Route drawing and closure pinning are separate map modes. A map click changes
  only the active mode's draft.
- The proposal includes an immutable before/after snapshot, reason, actor, and
  idempotency key. Approval remains an independent reviewer action.

## FDH cabinet ledger and editor

- Screen: `/admin/network/fdh-cabinets`; audience: authorized network operators.
- Job: find an active cabinet, inspect its identity and notes, and edit its
  descriptive fields.
- Read owner: the FDH cabinet list/detail projection. The list's total and page
  rows use the same active-cabinet scope and deterministic name ordering.
- List pagination reports the full matching total and the visible row range.
- Human notes remain visible at work depth. JSON import provenance is collapsed
  under an evidence disclosure.
- The editor accepts either a complete finite WGS84 coordinate pair on create,
  or no coordinates. Existing coordinates display zero correctly. Existing
  cabinet coordinate changes are submitted as reviewed movement proposals from
  the map rather than written by the descriptive edit form.
- Invalid coordinate input is a validation state, not an unhandled server error.

## Change request detail

- The first view summarizes the operation and affected asset.
- Raw payload and live snapshot remain available under collapsed evidence
  disclosures.
- Missing or stale live assets remain distinguishable from invalid requests.

## Validation evidence

Focused unit and adapter tests cover map proposal submission, rejection and
rollback behavior, mutually exclusive map modes, coordinate validation,
pagination totals, and provenance disclosure. Browser acceptance runs against
staging fixtures; production records are never used as test fixtures.
