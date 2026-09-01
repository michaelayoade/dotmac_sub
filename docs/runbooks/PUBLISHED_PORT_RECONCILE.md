# Reconciling an infrastructure service's published ports

> **Production not yet provisioned:** published-port reconcile v1 remains
> disabled. V2 is checked-in machinery, not permission to dispatch it. Do not
> dispatch PLAN or APPLY until every pre-dispatch obligation below has a live
> observation. No repository test can prove a host identity, sudo rule,
> environment reviewer, firewall collector, or external vantage exists.

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

The retained v1 **Reconcile infrastructure published ports** workflow is
intentionally disabled. V2 uses two different workflows and authority
surfaces:

- **Plan infrastructure published-port reconciliation** observes current
  protected `main` and the host through a dedicated read-only plan identity.
  That identity has no writable Docker socket and only one fixed sudo rule: an
  installed, isolated, root-owned observer whose bytes must match this exact
  source. The observer takes a service name from a root-owned allowlist and no
  caller-controlled path. It reduces resolved Compose in memory so neither
  resolved secrets nor container environment values enter artifacts or logs.
- **Apply authorized infrastructure published-port reconciliation** first
  observes two distinct successful first-attempt plan runs. The production
  environment approval is the separate authorization. Under the deploy lock,
  the host is planned a third time and byte equality is required before the
  root deadman is armed and before `.env` changes.

The v1 CLI emits canonical `PublishedPortIntentV1`, which is declared intent
only. A full `PublishedPortPlanV1` binds intent to immutable source identity and
typed host prestate. `PublishedPortPlanReceiptV1` binds one successful Actions
run and artifact to the plan and prestate digests. Receipts are evidence, not
authorization; authenticated initiator identity and production-environment
approval remain separate authority.

V2 enforces:

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

It additionally refuses a pull, build or dependency recreate; reuses the exact
running image ID and digest-pinned reference; proves both listener families;
and requires exact, independently sourced firewall and client-reach receipts
for every `(socket, required_client)` pair.

No current run or receipt satisfies those gates. Do not dispatch production.

### Pre-dispatch obligations

Every row is a measured host or GitHub fact, not a repository default:

1. Register a dedicated runner carrying `dotmac-sub-production-plan`. Its user
   must not be able to write `/var/run/docker.sock`, belong to a Docker-capable
   group, or hold general sudo.
2. Install `scripts/published_port_plan_observer.py` at
   `/usr/local/libexec/dotmac-published-port-plan-observer`, root-owned and not
   group/world writable. Its `#!/usr/bin/python3 -I` isolation is load-bearing.
   Grant sudo only for the two fixed `collect --service postgres-local` and
   `collect --service freeradius` argv vectors.
3. Create canonical, root-owned, non-writable
   `/etc/dotmac/published-port-plan-observer.json` with schema
   `PublishedPortObserverConfigV1`, target `dotmac-sub-prod`, project
   `dotmac_sub`, an absolute root-owned Docker binary, the measured deploy/env/
   Compose paths, and the sorted two-service allowlist. No workflow supplies a
   path to the observer.
4. On the APPLY runner, grant only the exact install/systemd/deadman operations
   in `scripts/reconcile_published_ports_v2.sh`, all through non-interactive
   `sudo -n`; retain Docker access there because this is the separately
   authorized mutating identity. Confirm the production environment has
   required reviewers and cannot self-approve.
5. Set `PUBLISHED_PORT_RECONCILE_V2_ENABLED=true`, `PRODUCTION_DEPLOY_DIR`,
   `PUBLISHED_PORT_RECONCILE_PYTHON_BIN`,
   `PUBLISHED_PORT_FIREWALL_PROOF_DIR`,
   `PUBLISHED_PORT_CLIENT_PROOF_DIR`,
   `PUBLISHED_PORT_FIREWALL_VERIFIER_IDENTITY`, and
   `PUBLISHED_PORT_CLIENT_COLLECTOR_IDENTITY` in their appropriate repository/
   environment scopes. The Python coordinate must resolve to an absolute,
   root-owned, non-group/world-writable interpreter whose isolated environment
   contains root-owned, non-group/world-writable Pydantic v2 and
   `pydantic_core`. The two proof identities must be different.
6. Provision two different root-owned proof stores, readable but not writable
   by APPLY. Provision the firewall verifier and an actual required-client
   vantage to write canonical receipts under the operation ID. The target host
   cannot mint its own reachability proof.
7. Prove the deadman timer survives a runner process kill and a reboot in the
   fake harness or an explicitly named disposable host before a maintenance
   window. Then produce two fresh PLAN runs. Review the canonical digest and
   open a distinct APPLY dispatch only inside the approved window.

The checked-in fake reboot/process-loss canary is `rehearsed` evidence only.
It proves a fresh process can consume the persisted root-state shape and take
the timeout rollback path; it does not label a systemd unit on a real boot as
`proved-live`. Record that separate cold-reboot observation before production.

For `postgres-local`, the exact obligation is TCP `9001 -> 5432` for
`75.119.157.91/32`. For `freeradius`, the exact obligations are UDP
1812/1813/1822/1823 for both `160.119.127.0/24` and
`102.220.189.0/24`. The declaration remains the source; these sentences are
review coordinates, not a second configuration list.

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
