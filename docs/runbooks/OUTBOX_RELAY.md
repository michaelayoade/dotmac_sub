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
`outbox_dispatcher`, the role is non-superuser/non-BYPASSRLS, it holds no
relation privilege in `public`, it can execute the tenant pair and it cannot
execute the platform pair.

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
`_dead_lettered_total`, `_parked_total`, `_lease_lost_total`,
`_cycle_errors_total`, `outbox_relay_last_poll_timestamp_seconds`,
`outbox_relay_oldest_pending_age_seconds`. Alerts:
`deploy/observability/outbox_relay.rules.yml`.

The dispatcher cannot read tables, so oldest-pending age is the oldest event this
process left pending (parked or retrying), retained for twice the park interval.
It is a floor on backlog age. Counters reset on restart; alert with `increase()`.
A true backlog gauge would need a third read-only SECURITY DEFINER function and
is out of scope.

## Operate

Not created by a normal deploy. Start by name (OPS-4):
`docker compose --profile outbox-relay up -d outbox-relay`. Rollback is
`docker compose stop outbox-relay`; claimed rows past the stale-lease window are
reclaimed by the next dispatcher (at-least-once). Handlers must be idempotent.

Inspect dead rows with an owner-role session:
`SELECT id, event_type, attempts, last_error FROM public.outbox_events WHERE status = 'dead';`
