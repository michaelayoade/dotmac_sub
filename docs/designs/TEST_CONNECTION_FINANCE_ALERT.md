# Native Test Connection Finance review

The source is the first-class `test_connection_grants` record owned by
`access.test_connection`, introduced by PR #3429/migration 645. Service Extensions,
free-text reasons, generic subscription resumes, and speed tests are not inputs.
This replaces the draft's pre-native Service Extension classification design.

## Atomic creation evidence

Activation retains all network-readiness, staff, fraud-hold, duration, expiry,
and commercial-state safeguards. After locking the subscription, the owner
takes a transaction advisory lock for the customer before selecting activation
time. This serializes counts across different subscriptions of one account.
The existing subscription lock and command ID still govern exact replays.

Each new native grant stages a deterministic `billing.test_connection.created`
event with tenant, customer, subscription and grant UUIDs, command provenance,
UTC boundaries, count, and up to ten immutable reference snapshots. Both record
and event commit or roll back together. The event remains for durable periodic
dispatch so creation never waits for Finance. The existing network-consequence
event and required expiry timer retain their original contract.

The creation timestamp is the native owner's `activated_at`: activation starts
immediately at write time, not a user-supplied historical schedule. Count
distinct grants over `(activated_at - 7 days, activated_at]`, including the
new grant. Expired, ended, and failed-delivery grants remain creations. Existing
native grant history in that period counts, without historical alert backfill.
Other customers and future/out-of-window records do not count. All subscriptions
of one canonical Subscriber account share the count. The native account/time
index bounds the query. No mutable counter or alternate grant ledger is added.

Counts and references are frozen in the event; asynchronous delay, expiry,
later creations, or retry cannot change the original decision. Same-command
replay emits no creation event and does not count twice.

## Workflow and delivery owners

Network Access Control Plane declares the creation trigger, `access.test_connection`
target and customer scope. Its authoring permission is the native
`subscription:test_connection` permission. Financial Access declares the
`billing.test_connection.notify_finance` action, requiring `notification:write`.
Configure `count_7d greater_than 5` and an explicitly selected Finance team.

`financial.test_connection_finance_review` validates the durable source event,
operator tenant, native grant/customer/subscription identity, published rule
version and exact configured action/team. It never activates access or calls a
network driver. Active Party-bound team members resolve through the existing
staff audience owner. Empty/unavailable recipients or missing email fail
visibly instead of reporting delivery success.

The command stages a recipient snapshot, in-app and email notices, typed-actor
audit, and contact-free queue evidence in one coordinator transaction. Messages
contain customer/account identity, count, dated UTC period, grant references,
duration and creator, and the exact subscription link. They request investigation
without asserting wrongdoing. Existing workers own provider delivery and retries;
queued is not delivered.

Receipt identity is event/rule-version/step and pins the selected team, evidence
digest and audience. Retry after action commit returns the original audience,
does not add newly joined staff or duplicate notices, and rejects changed
evidence. Each new qualifying sixth/seventh/etc. creation can alert; no unrequested
cross-occurrence cooldown is introduced.

## Schema and activation

Migration 646 follows `645_subscription_test_connection` and adds only Finance
receipt storage and constraints. It does not classify Service Extensions, change
the native grant schema, seed a workflow, or emit historical alerts. After review
receipts exist, downgrade refuses to erase them; correct forward.

The existing native staff Test Connection form is unchanged. Automation Center
uses its existing typed count condition and active-team selector. No client or
server script is needed. Deploy through the standard immutable staging gate,
then explicitly save/publish the approved workflow. Operator acceptance is in
`docs/runbooks/TEST_CONNECTION_FINANCE_ALERT.md`.

Validate native activation/expiry alongside 5/6 thresholds, exact time boundary,
customer isolation across multiple subscriptions, existing expired history,
creation replay, asynchronous/redrive behavior, receipt replay, authorization,
rollback, and migrated PostgreSQL concurrency/constraints. Keep the production
session-construction baseline unchanged; only the two reviewed PostgreSQL
test-factory sites enter the test-only inventory.
