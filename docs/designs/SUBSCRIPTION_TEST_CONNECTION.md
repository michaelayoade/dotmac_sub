# Subscription Test Connection

Status: native troubleshooting-access contract.

Owner: `access.test_connection`; network projection writer: `access.radius_projection`.

## Product behavior

Authorized staff open Test Connection below Invoice in the customer detail
page's All Subscriptions section. The action is outside the active-only Invoice
condition. It displays the administrator-configured system-wide duration and
expected UTC expiry before submission. Initiators cannot override the duration
for an individual customer or subscription.

`subscription:test_connection` is granted to Customer Experience Manager and
Finance Manager. Migration 645 grants it additively to existing named roles;
the seed catalog converges fresh installations. Other staff, including
engineers, receive the same dedicated permission through normal RBAC.

Outstanding invoices, prolonged arrears, insufficient funding, revoked billing
approval and missing legacy funding baselines cannot deny or shorten a test.
The command neither evaluates financial restoration eligibility nor writes
status, billing approval, money, service entitlements or billing anchors.
Reason-scoped locks, quota counters and throttled credential profiles remain
recorded. The effective test projection uses the subscription's full normal
profile, addresses and routes, then returns to current ordinary policy.

Explicit fraud/security holds remain effective. Canceled, hidden or archived
services and absent/unusable provisioning cannot be repaired by this action.
A provisioned pending, disabled or expired service can be tested. Shared
logins are evaluated against the selected subscription's network credential;
obsolete historical subscriptions do not block a test, while an unassigned
credential shared by another live subscription is refused rather than granting
another service/customer access.
The bounded network path requires RADIUS authentication against PostgreSQL;
non-RADIUS/static-only services need a separate NAS-native deadline capability.
The UI reports that prerequisite rather than claiming an activation succeeded.

## Record and transaction

`test_connection_grants` records the subscription/account, initiating active
staff principal and label, command identity, activation instant, duration and
absolute expiry. One open grant per subscription is enforced by the migrated
partial unique index. Activation locks the subscription; delivery/expiry lock
the grant. The interval begins at activation commit preparation, rounded to UTC
seconds; delayed network delivery consumes the remaining interval and never
silently restarts it.

Grant, actor-attributed subscription audit, version-1 event and required durable
expiry timer commit atomically through `execute_owner_command`. Request retries
use the submitted command UUID; changed replay inputs and overlapping requests
fail closed. Expiry addresses an exact historical grant, so an old timer cannot
expire a later test. An overdue open row is ended before a new grant is admitted.

The customer timeline explicitly displays activation, actor, duration, start
and expected expiry. It also records expiry. `pending`, `applied` and `failed`
delivery states are separate from the authoritative interval; the UI never
treats a queued network request as verified delivery.

## Effective access and network deadline

All application readers check the absolute interval against current UTC time.
`financial.access_resolution` changes only network eligibility for valid typed
grant evidence; its commercial/billable classifications remain unchanged.
Lifecycle retains the ordinary persisted derived access state. The test is a
clock-valid overlay, never a stored active state requiring a worker to reset it.
Connectivity reconciliation,
RADIUS planning, the writer and session-enforcement transports consume the same
evidence. Financial/FUP jobs may maintain underlying state during a test, but
cannot replace its full profile or apply their live-session/address-list block.

The sole RADIUS writer maintains ordinary rows plus a bounded override in the
same standard tables. Override attributes/group memberships use the reserved
`Dotmac-Test-` prefix; `Dotmac-Test-Until` is an absolute UTC epoch deadline and
`Dotmac-Test-Grant` identifies its source record. Prefix attributes are storage
metadata: the authorization queries strip/filter them before returning actual
RADIUS attributes. The exact fingerprint covers both ordinary and override
rows. Scoped deletion, owned-group cleanup and orphan repair cover both.

The checked-in PostgreSQL SQL module selects test check/reply/group rows only
before the deadline, and ordinary rows afterward. Each test authentication gets
`Session-Timeout` equal to the remaining period, never the original duration.
The default virtual server also caps the timeout after group processing and
rejects a test that expires during authentication. Its request-local
`Tmp-String-0`/`Tmp-Integer-0` attributes are reserved for this deadline guard.
Normal rows continue updating throughout testing, including payments and plan
changes. An expired override therefore falls back without waiting for a worker.

Network delivery projects all targets before refreshing sessions. Existing
sessions are disconnected using the confirmed enforcement path so reconnection
receives the full profile and deadline. Unsupported/failed terminal enforcement
is a retryable, visible delivery failure. Expiry recomputes current policy and
reconciles; it never restores a saved pre-test commercial-state snapshot.

## Deployment and verification

1. Apply migration 645 and deploy the application/worker together.
2. Deploy `config/freeradius/mods-enabled/sql` to every configured user-auth
   target and `config/freeradius/sites-enabled/default` to each authenticating
   server, adapting only configured table/schema names. Run `radiusd -C` and
   check the SQL module
   configuration and restart/reload FreeRADIUS through normal operations.
3. On a controlled provisioned staging subscription, verify overdue and missing
   baseline access, full IPv4/IPv6/routes/rate, a late reconnect's remaining
   timeout, confirmed existing-session refresh, and expiry while workers are
   stopped. Confirm each serving NAS actually honors Session-Timeout, uses
   RADIUS for reconnects and has synchronized time.
4. After every target/NAS in the served cohort passes, set the database-owned
   radius setting `test_connection_deadline_verified=true`. Its default is
   false: missing network configuration must not admit unbounded temporary
   access. This is a network capability attestation, not a financial-lifecycle
   enable/disable switch. Revoke the attestation if network topology/configuration
   changes invalidate the verified capability.
5. Configure `test_connection_default_hours` in the existing settings UI. This
   is the system-wide duration shown to every initiator and used for every new
   activation. `test_connection_maximum_hours` remains an administrator safety
   bound for that setting. Changing the default does not alter already
   committed grants.

Activation remains unavailable until the network capability is verified.
Rolling back application code during live tests requires first ending the
tests and reconciling their normal rows; retain the deadline-aware RADIUS SQL
until every prefixed override has expired or been removed. No destructive
financial cleanup/backfill is involved.

Validation: focused owner/timer/audit/permission/billing-independence tests;
migrated PostgreSQL constraints and actual RADIUS SQL deadline tests; UI and
architecture guards; full prescribed CI. Real NAS deadline behavior remains an
explicit staging acceptance gate and must not be inferred from SQL tests alone.

Protocol references: [FreeRADIUS SQL module](https://www.freeradius.org/radiusd/man/rlm_sql.html), [RADIUS Session-Timeout](https://www.freeradius.org/rfc/rfc3580.html).


## UI page contract

Screen `admin.subscription_test_connection` is a service-action editor for
permission-authorized support/engineering and finance staff. Its read and
command owner is `access.test_connection`; RBAC owns visibility and admission.
The first viewport shows the service login, configured whole-hour duration,
expected UTC expiry, current delivery status when present, and any unmet
prerequisites. The duration is read-only here; activating the displayed
system-wide interval is the single primary action. The account timeline holds
actor and interval evidence. The detail row links to the subscription's own account,
including when a person/company detail aggregates multiple accounts.

Unauthorized staff see no row action and both routes refuse their request.
Unavailable network capability and an already running test disable activation
with an explanation. Pending/applied/failed delivery is shown separately from
normal billing status. The editor stacks controls on mobile and uses the
existing admin shell, dark theme and form/CSRF conventions. No customer PII or
credential is displayed. This editor has no table, export, filter or bulk action.
