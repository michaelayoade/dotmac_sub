# Released Payments invoice intent observation

Owner: `financial.payment_intent_module_shadow`. This synchronous read runs in
`verify_and_record_payment` immediately after successful customer account
authorization and before provider selection/observation. It calls the released
`dotmac-payments==0.1.0a2` `get_payment_intent_v1` with the same caller Session
and `TenantScope(OPERATOR_TENANT_ID)`. The existing root Session hook installs
PostgreSQL tenant scope. The consumer creates no session, lock, transaction,
savepoint, mutable authority switch, write, flush or transaction completion.

The frozen legacy input is the authorized `TopupIntent`: payer/account,
original metadata invoice target including absence, invoice-settlement purpose,
requested Decimal amount, currency, storage scale, normalized provider/channel,
created/opened instant and original expiry including absence. Gateway invoice
creation freezes the invoice balance/total as requested amount; this read never
substitutes later provider gross, fees, confirmed money or allocation amounts.
The legacy scale of two is evidence from `Numeric(12,2)`, not currency policy.
Amount comparison never rounds. Currency case normalization follows released
Kernel `Currency` uppercasing; whitespace is not silently repaired. Provider and
channel normalize surrounding whitespace and case only. No new TTL is derived.
Metadata target remains observation input, with no new settlement authority.

The detached module observation is compared only on those binding facts.
Module status, confirmed money and settlement time carry no legacy state
mapping or settlement authority. Missing rows are observed gaps. Match, missing
and mismatch all leave the existing authorization/provider/settlement decisions
with their current owners. Database and unresolved-precision failures propagate;
the caller owns rollback, and the consumer never continues an aborted transaction.

`payment_intent_module_parity_total{outcome,field}` records one `field=none`
invocation and each mismatched field separately. Outcomes and fields are closed
vocabularies. No reference, identity, amount, provider value or customer data is
included. It is process-local request telemetry, not durable financial evidence;
a repeated authorized request rebuilds the comparison from current persisted
bindings. Finance operations owns drift triage; existing intent owners own any
separately reviewed repair. No backfill, import or dual write is authorized.

Hosted canaries cover each binding field, exact precision, absent targets,
original historical expiry, lifecycle independence, fail-closed database errors,
authorization/query/provider ordering and a real PostgreSQL `app_user` Session
using the actual application tenant hook with pending dirty state. Fixture
matching proves the seam only, not production coverage or an authority cutover.
Complete correlation/backfill, fleet parity and a separately approved global
switch remain prerequisites to retiring legacy payment/confirmation/allocation,
provider consequence and renewal owners.
