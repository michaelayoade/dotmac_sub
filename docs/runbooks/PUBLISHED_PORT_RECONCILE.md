# Reconciling an infrastructure service's published ports

> **Production refused:** published-port reconcile v1 is disabled. Do not
> dispatch the workflow or invoke the script against production, including with
> `--plan-only`. That branch writes `.env` before restoring it, so it is not a
> read-only plan. The workflow has only a hosted refusal job; it has no
> production environment, self-hosted runner job or reconcile invocation.

`scripts/reconcile_published_ports.sh` is the retained v1 implementation for
non-production rehearsal only. Declared intent lives in
`deploy/published_ports.toml`; the decision and v2 admission gate are in
[ADR-0014](../adr/0014-declared-published-port-intent.md).

## When you need this

`deploy.sh`'s `APP_SERVICES` covers the app, the celery workers, celery-beat,
`bandwidth-poller` and `syslog-listener`. Everything else — `postgres-local`,
`redis-local`, `nominatim`, `freeradius`, `genieacs`, `genieacs-mongodb`,
`victoriametrics` — is **never** recreated by a deploy. For those services a
merged change to `docker-compose.yml` does nothing to a running host until this
operation runs.

`scripts/published_ports.py list` prints the declared services and which knob
governs each port.

## Why it is not part of a deploy

Recreating the database on every release would be far worse than any binding
bug. A recreate of `postgres-local` briefly interrupts **every** database
connection. Treat it as scheduled maintenance.

## The ordering, and why it is enforced rather than documented

Recreating `postgres-local` while `PG_LOCAL_BIND` is unset applies compose's
loopback default, which binds the replication standby out of its own WAL
stream — turning a security fix into a replication outage. The script therefore
refuses to reach the recreate unless the environment value is in place *and
proven to be what compose actually resolves*:

| gate | what it does | what it prevents |
| --- | --- | --- |
| 0 | `APP_ENV`/`SERVER_NAME` must identify the named environment | reconciling staging as production |
| lock | takes the **deploy** lock (`flock`) | a recreate racing a deploy |
| 1 | `plan` refuses a bind that does not admit a declared required client | narrowing a port a live off-host client streams through |
| 2 | writes the env value, then **re-reads it out of `.env`** | trusting a write |
| 3 | `docker compose config --format json` must resolve the planned `host_ip` | the env value not actually taking effect |
| 4 | recreate, then assert the **container id changed** | `up -d` no-opping and reporting success |
| 5 | re-read **actual** listeners, both families | assuming the fix landed |

Gate 3 is the only place `VERIFIED_SERVICE` is assigned, and gate 4 names it.
Under `set -u`, removing gate 3 makes the recreate abort rather than run
unverified. `tests/architecture/test_published_port_reconcile_contract.py` pins
that.

## Production status and v2 admission

The **Reconcile infrastructure published ports** workflow is intentionally
disabled. Its sole job is a GitHub-hosted refusal. No production job or script
invocation remains in the workflow, so GitHub cannot request the production
environment or self-hosted runner. The script also refuses a production
argument before reading `.env` or calling Docker.

The v1 CLI emits canonical `PublishedPortIntentV1`, which is declared intent
only. A full `PublishedPortPlanV1` binds intent to immutable source identity and
typed host prestate. `PublishedPortPlanReceiptV1` binds one successful Actions
run and artifact to the plan and prestate digests. Receipts are evidence, not
authorization; authenticated initiator identity and production-environment
approval remain separate authority.

Production stays refused until a separately reviewed v2 provides:

- two distinct first-attempt terminal-success read-only plan runs with
  byte-identical canonical decision bytes;
- an immediate third read-only replan before apply and refusal unless its exact
  digest matches both prior plans;
- an exact-SHA, exact-digest, separately authorized apply using
  `docker compose up --no-deps` for only the target service;
- a root-owned persistent systemd deadman/timer and rollback bundle that
  survive runner death and reboot and restore `.env`, the target container and
  listeners; and
- normalized non-port service-definition equivalence plus proof that every
  non-target container ID is unchanged.

No current run or receipt satisfies those gates. Do not dispatch production.

## Non-production rehearsal

The retained script may be exercised only against an explicitly named
non-production environment. Its v1 ordering below is historical migration
context, not production authorization. `--plan-only` still changes `.env`
temporarily and must not be described as read-only.

### Exit codes

| code | meaning |
| --- | --- |
| 0 | reconciled: the service was recreated and now matches the declaration |
| 1 | refused: a gate failed |
| 2 | usage |
| 3 | **already reconciled** — nothing needed doing, no container was recreated |

3 is deliberately distinct from 0 so "the operation did nothing" can never read
as "the operation fixed it".

## Checking a host without changing it

```bash
ids="$(docker ps -q --filter label=com.docker.compose.project=dotmac_sub)"
docker inspect $ids --format \
  '{"service":{{json (index .Config.Labels "com.docker.compose.service")}},"container":{{json .Name}},"ports":{{json .NetworkSettings.Ports}}}' \
  > listeners.jsonl
```

Wrap those lines into a JSON array and run:

```bash
python3 -m scripts.published_ports check-listeners \
  --environment production --observed listeners.json
```

This reads **actual listeners**, not configuration, and compares both address
families. That matters: a bare publish's IPv4 half is correct, so a v4-only
check passes the exact defect this exists to catch.

## Adding a service to the declaration

1. Give the compose publish an explicit bind knob with a safe default —
   `${SOMETHING_BIND:-127.0.0.1:}PORT:PORT`. Never leave it bare.
2. Add a `[[publish]]` block to `deploy/published_ports.toml`. A non-loopback
   default requires `reach = "offhost"`, a `reason`, and `required_clients`.
3. Document the knob in `.env.example`.
4. Set `recreated_by_deploy` to match whether the service is in `deploy.sh`'s
   `APP_SERVICES` — a test holds you to it.

## What this does not cover

- **Authorization.** An address allowlist authenticates a network position, not
  a client. This governs which sockets exist.
- **The `DOCKER-USER` firewall**, which is IPv4-only here by construction: IPv6
  publishes are served by userland `docker-proxy` and terminate on `INPUT`,
  so a `DOCKER-USER` rule for them is silently dead. Do not read a v4 firewall
  rule as covering both families.
- **Non-Docker listeners** on the host. The check is scoped to the compose
  project.
