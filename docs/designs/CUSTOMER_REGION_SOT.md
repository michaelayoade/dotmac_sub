# Customer Region Source of Truth

`gis.customer_regions` owns configured customer-region records and the
deterministic assignment of a customer location to one winning region. Admin
routes are transport adapters: they authorize and parse requests, construct a
typed command, release read-only authorization transactions, and map domain
errors to responses.

The owner consumes the authorized command, the canonical customer service
location from `customer.accounts`, and NAS/session identity observations from
`network.identity`. A save or disable command enters
`execute_owner_command` exactly once. Existing rows are locked, helpers flush
without committing, and `customer_region.changed` is staged in the same
transaction as the configuration change.

Region assignment is a request-time projection. Its freshness is the database
snapshot used by the query. Missing coordinates produce no assignment rather
than a guess. Drift is detectable by comparing the SQL assignment relation
with `resolve_region` for the same inputs; repair is an idempotent rerun because
no assignment is persisted. Overlaps resolve by infrastructure match, match
mode, explicit priority, distance, and stable region identity in that order.
