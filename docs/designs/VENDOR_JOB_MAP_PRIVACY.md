# Vendor job destinations and map privacy

Status: implemented candidate; migrated PostgreSQL and authenticated device acceptance required.

## Authority and scope

`operations.field_work_order_access` resolves the active native SystemUser and
unambiguous FieldVendorUser/vendor binding. It owns the technician-only global
field-plant access policy. Vendor company ownership, capabilities, stale staff
profiles and coordinates never confer network inventory access.

`operations.field_jobs` owns the typed `FieldJobQuery` destination read and its
job-brief composition. A vendor must be the exact selected member on the current
assignment with a non-null assignment generation. The read acquires the same
user/work-order/member lock order as assignment admission, revalidates after
waiting, and never commits or writes. No expected generation is inferred from
client time, device coordinates or imported JSON. The server derives the current
generation from the authoritative assignment for every request.

Vendor destinations are the native work-order site plus explicit issued-outage
and native project targets. The existing `engineering_plan_origin` owner
resolves only this job's active Project and exact task. If a migration-676
issuance receipt exists, its project/target must agree with current scope;
retargeting the project cannot expand that issued job's map. Cabinet/site pins
come from exact native records. Device/OLT/PON identity without verified target
coordinates remains a label, with no guessed parent-site pin. Only coordinates,
labels and operational addresses are projected, never management addresses.

Proximity is not a relationship: vendor destinations never call the nearby
plant reader. Existing technician nearby-destination behavior is retained.
The global `/field/map-assets` list/nearby/search/location/revert adapters require
an active technician actor; vendor membership always prevents staff fallback.
The old vendor-wide nearby/search APIs return a refusal directing the user to an
assigned work order. The vendor web planner no longer receives a network plant
projection or exposes its plant search/filter controls. Its own scoped proposed
and as-built route evidence remains available under the existing vendor owner.

## Field map page contract

The vendor Map tab is **Assigned job locations**. Its audience is the selected
native vendor field member. The operational decision is where to travel for a
currently authorized job. The first view shows authorized job destination pins
and a labeled list. Selecting one opens its operational label/address and an
optional Navigate action through the existing external maps launcher. Precise
coordinates use directions; an address-only work site uses address search.
Missing coordinates never acquire a nearby or default plant target.

The mobile projection refreshes every thirty seconds and fetches fresh paginated assigned jobs and then each job's
canonical destinations. It does not request the device position or global
plant, persist map assets, or use an offline map fallback. A changed selection
or revoked job during loading fails the refresh. Refresh hides prior positions
while revalidating; an error does not show a previous map snapshot. Zero jobs
shows **No assigned job locations**. Unavailable coordinates retain an address
or label for the authorized job. Existing previously disclosed offline job
execution data is governed by its existing separate cache contract; this change
does not claim remote erasure of data already downloaded to a disconnected
client.

There is no vendor GPS upload, vendor movement tracking, company location feed,
customer tracking expansion, or migration in this slice. Reviewed identity and
photo/KYC ownership remain independent.

## Compatibility and validation

Technician APIs and navigation remain compatible. Vendor callers of the retired
global plant endpoints receive 403 and must update to assigned-job destinations.
The new Field map uses the already-existing jobs and destinations APIs. The
additive `project_target` job-brief direction source remains a server-provided
string in clients.

Fast unit/API cases exercise global API bypass (including stale technician
profiles), forbidden vendor plant mutation, nearby unrelated assets, exact
project target success, wrong job/member, reassignment, revocation and native
job navigation. Web render/architecture checks forbid restoring the global
projection. Flutter cases cover the request surface, navigation, empty state
and reassignment failure. PostgreSQL source reuses the real migrated clone
fixtures; it must run on the coordinator-owned dedicated test server. No local
Docker or metadata-based acceptance claim is allowed.
