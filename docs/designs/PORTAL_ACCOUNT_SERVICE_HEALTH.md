# Portal Account and Service Health

## Decision

`app.services.portal_account_health` is the read-only owner of the first-viewport
Account/Service Health projection used by Customer Portal, Reseller Portal,
Customer 360, and the customer mobile app. It composes authoritative owners; it does not poll
equipment, mutate lifecycle state, infer financial totals in a client, or create
a second outage decision.

The projection answers, for one exact subscriber account:

- account lifecycle and identity;
- effective billing mode;
- open receivables in separate currency lanes;
- prepaid service funding as a distinct fact, never netted with receivables;
- each operationally-current service's lifecycle and access decision;
- subscription-scoped RADIUS state, binding, IP, NAS evidence, and freshness;
- customer-safe connection/access-medium and area-outage diagnosis;
- any active plan-change intent, its target/effective date, and the owner-
  classified delivery mode, target address, payment/delivery state, and exact
  one-time field charge when applicable;
- charge/lapse dates and the canonical customer action.

Availability is explicit through `StateValue`. Unknown, unavailable, stale,
not-applicable, and authoritative zero are not interchangeable.

### Operationally-current subscription definition

`app.services.subscription_lifecycle_policy` owns the typed cohort decision
consumed by `customer.service_status`. A subscription is operationally current
when its lifecycle status is `pending`, `active`, `blocked`, `suspended`,
`stopped`, or `disabled`, except that a `stopped` or `disabled` row is
historical once its non-null explicit `end_at` is at or before the single,
timezone-aware `as_of` instant. The narrow end-date exception prevents an old
paused row from overriding a healthy replacement without treating end dates as
a general lifecycle authority.

Consequently, an `active`, `pending`, `blocked`, or `suspended` row with a past
end remains in customer health and in the drift report for review. A current
`disabled` or `stopped` row with no past end remains visible and keeps its
support action. Terminal lifecycle rows remain outside customer health. All
rows, including the historical paused rows omitted from customer health, remain
available to the admin Service/history query.

## Inputs and boundaries

The owner reads:

- account and subscription identity/lifecycle from their canonical records;
- billing mode from `financial.billing_profile`;
- receivables and prepaid funding from `customer.financial_position`;
- usability, reason, dates, and action from `customer.service_status`;
- live-session binding/freshness from `network.radius_sessions`;
- pending service-change intent from `service_intent.subscription_lifecycle`;
- customer-safe connection/outage diagnosis from `network.connection_health`;
- semantic labels/tones/icons from `ui.status_presentation`.

Account Health resolves one typed operational cohort at one `as_of` instant and
passes that exact cohort to Service Status. Neither composer maintains its own
status/end-date rule, and no UI surface reinterprets or mutates lifecycle state.

An exact RADIUS subscription binding wins. An unbound live session is eligible
only when the subscriber has exactly one operationally-current subscription.
It is never copied to sibling services. Page rendering never initiates device,
OLT, ONT, NAS, or RADIUS polling.

## Surface contracts

- Customer dashboard composes the shared Account Health projection with a typed,
  dashboard-only identity, currency-scoped unallocated-credit, and offer-speed
  read. The first viewport shows the canonical action when one exists, account
  credit separately from prepaid funding and receivables, and one service card
  per operationally-current subscription. A missing credit or speed read shows
  an explicit unavailable message. A missing action is described as no payment
  action only when Account Health is complete. The topbar remains owned by the
  shared customer layout. The dashboard also shows exact funded prepaid
  service-period progress, rated capped-allowance usage per service, customer
  support requests, and customer-safe invoice and connection updates.
- Customer service detail narrows the same account projection to the requested
  subscription after the existing ownership check. If an owned subscription is
  missing from that projection, the detail service returns no result so the
  route responds with not found; it does not render data from a partial or
  mismatched projection.
- Reseller account detail performs its reseller/account scope check before
  building the projection and uses the same macros.
- Admin Customer 360 renders the same service-health strip before its tabs,
  narrowed by the projection owner to active services only. Historical,
  disabled, suspended, and otherwise non-active subscriptions remain available
  in the Service tab; the template does not filter lifecycle state or maintain
  an independent connection or change-status summary.
- Mobile calls `GET /api/v1/me/account-health` and renders the transport schema
  in `app.schemas.portal_account_health`.

### Customer dashboard page contract

- Screen: `/portal/dashboard`, customer glance dashboard. The customer decides
  whether an owner-confirmed action is needed and which service to inspect.
- Primary identity: authenticated account number and operationally-current
  subscription IDs. The customer session scopes the read before projection.
- Read owner: `ui.portal_account_health_projection`; dashboard highlights use
  its typed `CustomerDashboardHighlightsQuery` and result. Source records are
  the account, currency-scoped unallocated-credit ledger, scoped offers, funded
  service entitlements, exact taxed prepaid renewal charge, reviewed prepaid
  funding, customer chargeability and billing treatment, rated quota buckets,
  canonical support-ticket queries, issued invoice records, and the shared
  connection projection.
- First viewport: local greeting, account lifecycle, canonical primary action or
  seven-day renewal warning or confirmed area outage,
  account credit, and per-service lifecycle, access, session, connection,
  plan speed, next charge date, connection evidence time, funded service period,
  rated capped-allowance usage, open support requests, and relevant invoice and
  connection updates.
- Actions: the primary action uses the service-status owner's label and URL;
  the dashboard-only advance-renewal action links to prepaid top-up. It starts
  at most seven 24-hour days before the confirmed renewal date only when the
  exact contracted charge exceeds available prepaid funding. The projected
  shortfall is the cumulative amount needed for renewals due by that date;
  confirmed free or customer-billing-suppressed services do not request funds.
  Existing active service-status actions take precedence, then the advance
  renewal action, then a confirmed area-outage message, then the clear state.
  The area-outage message requires a current confirmed diagnosis, is exactly
  "There is an outage in your area", and has no payment action. When neither
  applies and all required reads are complete, including current connection
  diagnoses, the card says "No urgent action is needed right now." A failed or
  ambiguous near-term renewal quote never becomes a payment CTA or a clear-state
  claim.
  This reminder does not change prepaid enforcement, grace, or suspension.
  The existing billing history, service details, support requests, invoice review, and
  notifications are read-only drill-downs. Chat opens the existing portal chat
  widget only when enabled for a writable customer session; otherwise the
  existing support page is the destination. View-only sessions do not show a
  payment or chat action. No destructive or bulk action exists.
- Availability: absent account health shows a page warning; unavailable credit,
  speed, charge date, or connection shows an explicit message. Confirmed zero
  credit remains zero. Multiple currencies do not collapse into one credit
  amount. A period progress bar appears only for one unambiguous active funded
  entitlement. Usage appears only for one current quota bucket matching the
  service's offer allowance; an uncapped bucket is identified separately.
  Missing or ambiguous evidence shows an unavailable message. No operational
  service yields a dedicated empty state. A confirmed zero open-ticket count is
  shown as zero; a failed support read says unavailable. Invoice activity uses
  only active, non-proforma, issued or partially paid or overdue invoices with
  a positive balance. Its due date appears only with verified due-date basis.
  A failed invoice read or missing connection diagnosis cannot erase a confirmed
  update from the other owner and is marked as a partial feed. No invoice or
  connection update has a distinct empty message.
- Freshness and provenance: Account Health and connection observations carry
  their own timestamps. The dashboard does not poll equipment or cache a second
  status decision. The reminder requires one active funded entitlement whose
  end matches the subscription billing anchor, and quotes the canonical taxed
  contracted renewal charge at that end instant. It compares the charge with
  the reviewed available prepaid funding in the same currency. A missing or
  conflicting entitlement, anchor, price, currency, or funding read fails
  closed. Dates display in Lagos time. Sensitive financial amounts
  remain inside the authenticated customer session.
- Layout: the same facts stack on mobile. From the medium breakpoint, each
  service card spans the full available width. The first usage and support cards
  share a row beneath the first service card, giving both cards room for their
  details without stretching them to service-card height. Additional service
  and usage cards follow below. The shared customer layout owns the topbar, and
  the update rows follow the cards.

Templates and mobile clients may choose navigation and native layout. They do
not derive billing position, access eligibility, session freshness, outage
state, or next-action meaning.

## Coordinated cutover and retirement

This is an explicit cutover, not a compatibility phase. The following parallel
contracts are retired after all in-repository callers move:

- `GET /api/v1/me/service-status`;
- `GET /api/v1/me/connection-status`;
- the mobile `ServiceStatus` model and its separate repository/provider;
- customer page-local accounting-session freshness;
- reseller `open_balance` presentation and template status mapping;
- customer dashboard generic account balance and invoice-cache aggregation.

`app.services.service_status` remains the internal policy/resolver owner of
customer-visible usability and action hints. It is composed by Account Health;
it is no longer a separate mobile response contract.

## Performance contract

The account projection is one request and receives the exact current service
cohort once. Live sessions are batch-resolved for that cohort. Full topology
diagnosis runs only for active services. No historical subscription receives a
diagnosis and no client issues a second connection-status request.

The customer dashboard highlights read reuses that projection and batch-loads
offer speeds for its already-scoped service IDs. It reads ledger credit in one
currency lane; it never nets credit against receivables or infers a payment
amount from a future charge. A separate account-credit read failure does not
replace a confirmed zero or fail the whole page.

The dashboard batch-reads active funded entitlements for exact prepaid service
periods and current rated quota buckets for capped usage. It never infers a paid
period from `next_billing_at`, merges multiple quota windows, or treats an
unlimited bucket's zero as measured usage. Quota usage carries the bucket's
recorded time and may lag live traffic. Missing or conflicting source evidence
does not produce a percentage or fabricated data amount. The new entitlement
and quota reads are each batched across the already-scoped service IDs.

The seven-day reminder reuses those exact periods and the Account Health
prepaid-funding read. It groups eligible renewal dates, checks free-service and
billing-treatment decisions, and requests canonical contracted charges only
for groups in the window. The first cumulative funding shortfall becomes the
dashboard advisory. An unavailable dependency suppresses a financial CTA.
These bounded dashboard reads do not change the separate Account Health query
budget or perform a write.

The dashboard support card uses the support owner's canonical customer scope
and non-closed status scope. The previous dashboard-context open-count query,
which turned a failed read into zero, is retired. Invoice activity reads one
customer-scoped open invoice. Connection activity reuses the Account Health
diagnosis already loaded for each current service and adds no network poll.

The one-active-service fixture currently uses 26 SQL statements and is guarded
at a maximum of 28. Any increase must identify the new authoritative input and
update this document and the test together. Additional-service scaling requires
a separate measured budget before a page is allowed to diagnose an unbounded
cohort.

## Drift, repair, and rollback

`app.services.service_status.build_subscription_end_drift_report` is the read-only
drift signal for non-terminal subscriptions whose explicit `end_at` has passed.
For an explicit `as_of` and optional subscriber scope it returns deterministically
ordered typed rows and a stable SHA-256 fingerprint. Historical paused rows are
classified as having one newer active same-offer replacement, no such
replacement, ambiguous replacements, or unavailable chronology. Other
non-terminal statuses fail closed as requiring review.

This slice introduces no automatic reconciliation writer and changes no
subscription, billing, RADIUS, enforcement-lock, or audit state. If an operator
later repairs a reviewed row, the only permitted write path is the public typed
`access.subscription_lifecycle` expiration command with explicit actor, reason,
effective time, reviewed head, and idempotency key; direct SQL and blind Alembic
data updates are forbidden. A bulk apply adapter requires a separate owner
contract and runbook before use.

Cutover removes the private `service_status._CURRENT_STATUSES` cohort and the
independently timed Account Health read. Rollback is code-only: restore the
prior reader behavior. No data rollback is required because this projection
change performs no writes. Drift evidence remains available throughout cutover.

## Verification

- financial currency-lane and unavailable-versus-zero tests;
- exact/unbound multi-service RADIUS binding and non-leakage tests;
- shared Customer/Reseller/Customer 360 template boundary tests;
- API route retirement and mobile Account Health model tests;
- template compilation, SOT manifest, and focused backend/mobile tests.
- deterministic past-end drift fingerprint and fail-closed ambiguity tests.
