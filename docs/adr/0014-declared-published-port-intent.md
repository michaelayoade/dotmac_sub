# ADR 0014: Declared publish intent owns every published host port

Status: accepted

Date: 2026-08-31

Decision owner: Michael Ayoade

Affected systems and domains: `docker-compose.yml`, `scripts/deploy.sh`,
`scripts/reconcile_published_ports.sh`, host firewall (`DOCKER-USER`),
production and staging deployment hosts.

## Context

On 2026-08-31 `postgres-local` published port 9001 on `0.0.0.0` **and** `[::]`.
The IPv4 listener was source-restricted to the replication standby by a
`DOCKER-USER` rule. The IPv6 listener was governed by nothing, and
`[2a02:c204:2334:8415::1]:9001` had been reachable from the public internet for
at least 41 days. Transport was `ssl = off`.

Three separate properties combined to produce that, and each is a general
problem rather than a fact about 9001:

1. **A bare publish is dual-family, and invisible.** `- 9001:5432` names no
   host address, so Docker starts one `docker-proxy` on `0.0.0.0` and a second
   on `[::]`. Neither address appears in any file. Grepping compose for
   `0.0.0.0` finds nothing; the exposure came from an *absence*.

2. **The firewall cannot govern the v6 half.** IPv4 publishes are DNAT'd and
   traverse `FORWARD` → `DOCKER-USER`. IPv6 publishes here are served by
   userland `docker-proxy` and terminate on `INPUT`, so a `DOCKER-USER` rule
   for them is silently dead. The v4/v6 chains are asymmetric, and treating
   them as symmetric is how a port looks governed while being open.

3. **There was no managed way to change an infrastructure service's ports at
   all.** `scripts/deploy.sh`'s `APP_SERVICES` deliberately excludes
   `postgres-local`, `redis-local`, `nominatim`, `freeradius`, `genieacs` and
   the metrics services, so no deploy recreates them. PR #2845 merged the
   correct compose default and *nothing happened*, because a merged repository
   change is not a changed host. The only remaining option was a hand-edit on a
   box whose checkout is already 16 tracked files divergent from `origin/main`
   — unattributable, and it makes the eventual descriptor work harder.

Configuration checks cannot catch (1): the file is what is missing. Firewall
audits cannot catch (2): the rule that would be audited is in the wrong chain.

## Decision

`deploy/published_ports.toml` is the single authoritative declaration of every
host port this repository publishes: the service, port, protocol, the env knob
that sets its bind, its default, its per-environment value, whether it needs
off-host reach, and which clients must retain a path.

Three consumers, one declaration:

- **`scripts/published_ports.py check-compose`** — offline, in CI. Compose's
  publish specs must match the declaration, and no publish may be bare.
- **`scripts/published_ports.py check-listeners`** — reads the host's ACTUAL
  listeners (`docker inspect`'s `HostIp` values) and compares them to the
  declaration **in both address families**. Configuration is not evidence; a
  listener is.
- **`scripts/reconcile_published_ports.sh`** — the only managed way to apply a
  published-port change to a service a deploy does not recreate. Requested and
  recorded through the "Reconcile infrastructure published ports" workflow.

The bind shape is default-deny: every publish names an address, through a knob,
with a safe default. Loopback is the default wherever the service has no
off-host client. The two collectors whose clients are off-host by definition
(`freeradius`, `syslog-listener`) default to an explicit IPv4 wildcard, which is
still a narrowing — it removes the ungoverned `[::]` half — and requires a
declared `reach = "offhost"` with a reason and named required clients.

An address allowlist authenticates a network position, not a client. This ADR
governs *which sockets exist*; it does not claim to be an authorization model,
and it does not change `ssl`, `pg_hba`, or any credential policy.

## Decision amendment — 2026-08-31 (v1 production executor disabled)

The v1 production executor is disabled. Its `--plan-only` branch changes
`.env`, asks Compose to resolve the changed file, and restores the preimage on
exit. Restoration does not turn a mutation into a read-only observation, so a
v1 run is neither admissible production plan evidence nor authorization. The
"Reconcile infrastructure published ports" workflow now contains only one
GitHub-hosted refusal job: there is no production environment, self-hosted
runner job or reconcile invocation to acquire. The script independently refuses
`--environment production` before it reads `.env` or calls Docker.

The versioned contract is split into three typed objects with different roles:

- `PublishedPortIntentV1` is the canonical declaration decision emitted by the
  existing CLI. It contains the service, environment, bind assignments and
  declared target sockets. It is not an apply plan.
- `PublishedPortPlanV1` binds that intent to the exact protected-main source
  SHA, repository, workflow, production target, change reference and reason,
  declaration and Compose digests, plus typed observed prestate: the target
  container and image, actual listeners, a versioned non-port service
  projection digest, and the project container-ID map.
- `PublishedPortPlanReceiptV1` identifies one terminal plan workflow run and
  artifact and binds the canonical plan and prestate digests. Two matching
  receipts are evidence only. They are never an authorization receipt and
  never replace authenticated initiator identity or production-environment
  approval.

Production remains refused until a separately reviewed v2 structurally splits
plan and apply and proves all of the following:

1. A plan is host-read-only: it cannot write `.env`, change Docker, or alter
   firewall state. Checkout and every decision input are pinned to an exact
   protected-main SHA.
2. Two distinct terminal-success plan runs, each on its first attempt, publish
   byte-identical canonical `plan.json` decisions. Run envelopes may differ;
   decision bytes may not.
3. Apply retains a distinct authenticated authorization reference and performs
   an immediate third read-only replan. It refuses unless those bytes and the
   exact digest match both prior plans.
4. Before the first mutation, a root-owned persistent deadman worker and
   `Persistent=true` systemd timer are installed, verified and armed, with
   root-owned state and a rollback bundle under
   `/var/lib/dotmac/published-port-reconcile/`. Explicit failure rolls back
   immediately; runner death or reboot is caught by the timer.
5. Rollback restores the bind-key preimage, target container and actual
   listeners. Disarm happens only after complete readback.
6. Apply uses `docker compose up --no-deps` for exactly the target service,
   proves the normalized non-port service definition is equivalent, and proves
   every non-target project container ID is unchanged. Only the target
   container ID may change.

V2 remains a narrow production executor with an explicit retirement gate into
the deployment controller path; it does not become the permanent deployment
authority merely because the safety gates pass.

## Invariants

- No publish in `docker-compose.yml` is bare. Every one names a host address.
- A non-loopback bind exists only under `reach = "offhost"` with a `reason` and
  non-empty `required_clients`.
- A bind that does not admit a declared required client is refused at plan
  time, before anything reaches a host.
- The listener check compares every `HostIp`, classified by family. A check
  that inspects only IPv4 does not satisfy this ADR.
- An environment with no declaration is refused, never assumed to take defaults.
- A listener with no declaration is a finding, not an omission.
- `recreated_by_deploy` in the declaration matches `deploy.sh`'s `APP_SERVICES`.
- The reconcile proves the environment value is what compose actually resolves
  **before** recreating, and proves the container id changed **after**.

## Consequences

- A published-port change to an infrastructure service becomes a named,
  attributable operation with an audit trail, instead of an SSH session.
- The listener check found five further instances of the same class on the
  first run (`freeradius` 1812/1813/1822/1823 and `syslog-listener` 514, all
  dual-family). They were pre-existing and unnoticed.
- `syslog-listener` **is** in `APP_SERVICES`, so its bind change rides the next
  ordinary release rather than needing a reconcile. That drops its `[::]:514`
  listener. Routers and OLTs ship syslog over IPv4, so the v4 path is unchanged.
- Recreating `postgres-local` briefly interrupts every database connection. That
  is why this is a scheduled maintenance action and explicitly not a deploy step.
- Rejected: making the deploy reconcile ports on every run. Recreating the
  database on each release would be far worse than the bug being fixed.
- Rejected: a compose-text grep for `0.0.0.0`. It finds nothing here — the
  defect is a missing token, and the exposure is created at runtime by Docker.
- Rejected: adding an `ip6tables` rule for 9001. It would sit in a chain the
  traffic never traverses, producing a rule that reads as protection.

## Migration and cutover

- **Old owner and paths:** none. Published ports were literal strings in
  `docker-compose.yml`, applied by whoever last recreated a container, with two
  ad-hoc knobs (`VM_BIND`, `PG_LOCAL_BIND`) and no check of either.
- **New owner and paths:** `deploy/published_ports.toml` owns intent and
  `scripts/published_ports.py` enforces it. V1 apply is disabled in production;
  no production executor is currently admitted.
- **Backfill/repair:** refused until the v2 gates in the amendment above exist.
- **Shadow or verification phase:** no v1 production shadow phase is admitted;
  `--plan-only` mutates `.env` temporarily and therefore is not read-only.
- **Cutover gate and evidence:** two identical terminal v2 plan decisions plus
  the immediate matching replan are required evidence, separate from production
  authorization. No such evidence exists yet.
- **Fallback retirement:** v1 stays disabled and is removed only after v2 is
  admitted and then retired into the deployment controller path.
- **Schema contract step:** not applicable — no database schema changes.

## Verification

- `tests/test_published_ports.py` — the comparison logic, with both directions
  planted: the real production listener set is refused, the corrected set is
  admitted, and `test_a_v4_only_comparison_would_have_admitted_the_real_defect`
  shows the same input passing a v4-only comparison and failing this one.
  Declaration validation refuses a bare bind, an undeclared non-loopback bind,
  and an off-host publish with no required clients — each with a companion test
  proving the refusal is not refusing everything.
- `tests/architecture/test_published_port_reconcile_contract.py` — the ordering
  is structural: `VERIFIED_SERVICE` is assigned exactly once, by the
  effective-compose gate, and the recreate names it, so dropping that gate makes
  the recreate abort under `set -u`.
- CI runs `check-compose` offline; the reconcile workflow runs `check-listeners`
  against the host.

## Rollback or forward-fix

- A failed gate before the recreate restores `.env` and changes no container.
- After the recreate `.env` is deliberately **not** rolled back: the planned
  value is the value the host should hold, and reverting it would leave the file
  disagreeing with the running container.
- Reverting a bind is the same operation with the declaration changed — never a
  hand-edit on the host.
- Not reversible by this mechanism: the connection interruption a recreate
  causes. Schedule it.

## Review and retirement

- Review date: 2027-02-28
- Retirement condition: superseded when Sub adopts
  `dotmac-deployment-foundation`'s `IngressPolicy.v1`, which owns declared
  exposure fleet-wide. This ADR is the Sub-local expression of the same rule and
  should migrate rather than persist alongside it.
- Supersedes or is superseded by: none.
