# Customer field identity disclosure

`customer.field_identity` owns read-only customer disclosure. Its typed queries
carry the authenticated subscriber UUID and native work-order UUID; portrait
queries additionally carry the card's assignment generation and portrait
revision. The adapter never accepts a subject UUID or stored-file ID.

Every card and portrait read rechecks exact work-order subscriber ownership,
customer job kind, the work-order owner's typed current-assignment query and
non-null generation. A native technician must be active and explicitly bound
through `TechnicianProfile.system_user_id` to an active existing `SystemUser`.
The neutral `person_id`, imported identifiers and vendor owner never identify crew.
Vendor assignments disclose only the explicitly selected active FieldVendorUser
linked to an active SystemUser and the independently reviewed identity revision.
Legacy organisation-only assignments return `actual_crew_not_selected`; no owner
or company member is inferred.

The query discovers a candidate without locks, then takes shared SystemUser,
technician profile where applicable, work-order, and selected vendor membership
locks in that order, matching field transitions'
user-before-visit ordering. It refreshes and rechecks ownership, bindings and the
exact assignment after any wait; a changed candidate fails closed. Portrait retrieval rechecks generation before consulting the identity
owner for the exact current reviewed revision. Reassignment, generation loss,
profile deactivation, replacement, rejection or revocation refuses the old
portrait. These checks protect request admission; a response already delivered
cannot be recalled. No local cache supplies identity authority.

Customer cards expose only approved display name/company, typed reviewed status
and review timestamp, portrait revision and assignment generation. They contain
no raw verification evidence, review reasons, subject identifiers or stored-file
identifiers. An unavailable card has an explicit typed reason. A denied customer
scope returns the same not-found outcome as a missing work order.

The authenticated `/api/v1/me/work-orders/{UUID}/field-identity` card and
`/field-identity/portrait?assignment_generation={UUID}&portrait_revision={int}`
stream routes use private, no-store responses. The portrait stream begins only
after owner policy passes; generic file routes continue to reject identity files.
The identity owner returns newly encoded, metadata-free PNG pixels, applying
EXIF orientation for both normalized uploads and previously reviewed originals.
The private object is fully read within bounded byte/pixel/dimension limits and
validated before any customer response begins; decoding failures refuse delivery.
No new schema, provider, grants, public URLs or real identity collection is added.

Fast unit tests are non-authoritative; the integrated 671/672 migration and
assignment concurrency acceptance remain PostgreSQL test-server gates.
