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
  shared customer layout. The later dashboard slice adds exact service-period
  progress, usage, support, and recent activity.
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

### Customer dashboard page contract, first slice

- Screen: `/portal/dashboard`, customer glance dashboard. The customer decides
  whether an owner-confirmed action is needed and which service to inspect.
- Primary identity: authenticated account number and operationally-current
  subscription IDs. The customer session scopes the read before projection.
- Read owner: `ui.portal_account_health_projection`; dashboard highlights use
  its typed `CustomerDashboardHighlightsQuery` and result. Source records are
  the account, currency-scoped unallocated-credit ledger, and scoped offers.
- First viewport: local greeting, account lifecycle, canonical primary action,
  account credit, and per-service lifecycle, access, session, connection,
  plan speed, next charge date, and connection evidence time.
- Actions: the primary action uses the service-status owner's label and URL;
  billing history and service details are read-only drill-downs. View-only
  sessions do not show a payment action. No destructive or bulk action exists.
- Availability: absent account health shows a page warning; unavailable credit,
  speed, charge date, or connection shows an explicit message. Confirmed zero
  credit remains zero. Multiple currencies do not collapse into one credit
  amount. No operational service yields a dedicated empty state.
- Freshness and provenance: Account Health and connection observations carry
  their own timestamps. The dashboard does not poll equipment or cache a second
  status decision. Dates display in Lagos time. Sensitive financial amounts
  remain inside the authenticated customer session.
- Layout: the same facts stack on mobile. The shared customer layout owns the
  topbar. Later slices add only owner-backed service-period, usage, support, and
  activity sections.

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
