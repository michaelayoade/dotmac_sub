# Portal Account and Service Health

## Decision

`app.services.portal_account_health` is the read-only owner of the first-viewport
Account/Service Health projection used by Customer Portal, Reseller Portal, and
the customer mobile app. It composes authoritative owners; it does not poll
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
- charge/lapse dates and the canonical customer action.

Availability is explicit through `StateValue`. Unknown, unavailable, stale,
not-applicable, and authoritative zero are not interchangeable.

## Inputs and boundaries

The owner reads:

- account and subscription identity/lifecycle from their canonical records;
- billing mode from `financial.billing_profile`;
- receivables and prepaid funding from `customer.financial_position`;
- usability, reason, dates, and action from `customer.service_status`;
- live-session binding/freshness from `network.radius_sessions`;
- customer-safe connection/outage diagnosis from `network.connection_health`;
- semantic labels/tones/icons from `ui.status_presentation`.

An exact RADIUS subscription binding wins. An unbound live session is eligible
only when the subscriber has exactly one operationally-current subscription.
It is never copied to sibling services. Page rendering never initiates device,
OLT, ONT, NAS, or RADIUS polling.

## Surface contracts

- Customer dashboard renders the shared financial and service-health macros.
- Customer service detail narrows the same account projection to the requested
  subscription after the existing ownership check.
- Reseller account detail performs its reseller/account scope check before
  building the projection and uses the same macros.
- Mobile calls `GET /api/v1/me/account-health` and renders the transport schema
  in `app.schemas.portal_account_health`.

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

The one-active-service fixture currently uses 26 SQL statements and is guarded
at a maximum of 28. Any increase must identify the new authoritative input and
update this document and the test together. Additional-service scaling requires
a separate measured budget before a page is allowed to diagnose an unbounded
cohort.

## Verification

- financial currency-lane and unavailable-versus-zero tests;
- exact/unbound multi-service RADIUS binding and non-leakage tests;
- shared Customer/Reseller template boundary tests;
- API route retirement and mobile Account Health model tests;
- template compilation, SOT manifest, and focused backend/mobile tests.
