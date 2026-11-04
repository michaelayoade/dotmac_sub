# Outbox relay dispatcher (SUB-2)

`python -m app.outbox_relay` drains `public.outbox_events` (the composed-module
tenant outbox from migration `557_outbox_relay_prereq`) through the two
SECURITY DEFINER functions `claim_outbox_batch` / `settle_outbox_event`. It is
not Sub's `event_store`, notification, network-operation or field-ERP outboxes.

## Scope and plane

Tenant plane only. `app/migration_bindings.py::ASSEMBLY_MODULE_PLANES` selects
`ModulePlane.TENANT` for billing, collections, numbering and subscriptions, so
nothing enqueues to `platform_outbox_events` and no platform dispatcher is run.
Selecting a module on the platform plane later requires a second dispatcher in
the same change.

## Credential and role

The only credential is `OUTBOX_RELAY_DATABASE_URL`, the `outbox_dispatcher`
LOGIN role (EXECUTE on the tenant claim/settle pair, USAGE on `public`, no
table or column privilege, no BYPASSRLS). The bootstrap
(`scripts/bootstrap_outbox_dispatcher_roles.py`) creates the role but never sets
a password; the password and URL are entered as a typed deploy parameter at ship
time, not written to a file. The service takes no `env_file` and no
`DATABASE_URL`.

At start-up the process refuses (exit 2) unless it is connected as
`outbox_dispatcher`, the role is non-superuser/non-BYPASSRLS, it holds **no
table or column privilege in any non-system schema** (not only `public`, so
`mod_*` schemas are covered), it is a **member of no other role**, it can execute
the tenant pair, and the **only SECURITY DEFINER functions it can execute** are
that pair (PUBLIC holds EXECUTE by default, so this catches any other definer and
the platform pair).

### Tenant credential (second connection)

Handlers have the kernel's `(event, tenant_db)` shape. `tenant_db` is a fresh
session on `OUTBOX_RELAY_TENANT_DATABASE_URL`, an `app_user`-class RLS role (never
the dispatcher), with `app.current_tenant` restored to the event's tenant for the
transaction; the relay commits it on success and rolls it back on error. The
variable must differ from the dispatcher URL. It is optional while the registry
is empty and is **required (start-up refuses) once any handler is registered**.
It is supplied with the first handler, together with a preflight for that role.

## Handler registry rule

`app/outbox_relay/registry.py`. An event whose type has no registered handler is
**parked**: returned to `pending` with `available_at = now + OUTBOX_RELAY_PARK_SECONDS`
(default 300), attempts unchanged, `last_error = 'no handler registered for event
type'`. It is never failed and never dead-lettered, so it is delivered the moment
a handler is registered and the relay is redeployed. Dead-letter happens only
when a registered handler keeps raising (kernel policy: capped exponential
backoff, dead at `OUTBOX_RELAY_MAX_ATTEMPTS`, default 8). Only the exception
class name is stored in `last_error`. The shipped registry is empty.

## Metrics (port 9108, scraped by vmagent job `dotmac-outbox-relay`)

`outbox_relay_claimed_total`, `_succeeded_total`, `_failed_total`,
`_dead_lettered_total`, `_parked_total{event_type}` (at most 50 distinct values, then `other`), `_lease_lost_total`,
`_cycle_errors_total`, `outbox_relay_last_poll_timestamp_seconds`,
`outbox_relay_oldest_pending_age_seconds`. Alerts:
`deploy/observability/outbox_relay.rules.yml`.

The dispatcher cannot read tables, so oldest-pending age is the oldest event this
process left pending (parked or retrying), retained for twice the park interval.
It is a floor on backlog age. Counters reset on restart; alert with `increase()`.
A true backlog gauge would need a third read-only SECURITY DEFINER function and
is out of scope.

## Database-side backlog

The relay cannot read tables, so the main app publishes the backlog itself: the
`outbox_backlog_snapshot` scheduled task (every 300 s, task
`app.tasks.outbox_backlog.publish_outbox_backlog`) reads `public.outbox_events`
under the operator-tenant scope and publishes
`observability_state{domain="outbox_backlog"}` signals `pending`, `claimed`,
`dead`, `oldest_pending_age_seconds`. `OutboxEventsQueuedRelayNotRunning` fires
when the oldest pending age is over 15 minutes and no relay metrics exist, so it
is silent on a host with an empty outbox and does not use a bare `absent()`.
`OutboxDeadEventsPresent` persists across relay restarts.

## Unconsumed types

Parked types raise `OutboxEventTypeParked` after 1 h (warning) and
`OutboxEventTypeParkedLong` after 24 h (critical). A type Sub deliberately does not
consume is declared with `registry.acknowledge_without_consumer(type)`; it then
settles as `sent`. Phase B PASS 10 requires zero parked rows of any type.

## Deviation D6 (named, deletion-registered)

The Sub-local loop in `app/outbox_relay/dispatcher.py` duplicates the kernel's
`messaging.worker.run_forever` because the kernel has no park outcome and
`ClaimedEvent` has no `created_at`. Michael decision D6 is pending; this is built
as recommended. Deletion condition: a Starter kernel release adds
`DeliveryOutcome.PARK` and `ClaimedEvent.created_at`; at the next Sub kernel bump
switch to the kernel worker and delete `run_forever`, `run_cycle`, `claim_rows`
and `park_event`. The handler shape, two-credential split and registry are already
the kernel's, so handlers do not change.

## Operate

Not created by a normal deploy. The first start ships with the change that sets
`replicas = 1` in `deploy/product.toml` and removes `outbox-relay` from
`OPT_IN_APP_SERVICES`. Start by name (OPS-4):
`docker compose --profile outbox-relay up -d outbox-relay`. Rollback is
`docker compose stop outbox-relay`; claimed rows past the stale-lease window are
reclaimed by the next dispatcher (at-least-once). Handlers must be idempotent.

Inspect dead rows with an owner-role session:
`SELECT id, event_type, attempts, last_error FROM public.outbox_events WHERE status = 'dead';`
