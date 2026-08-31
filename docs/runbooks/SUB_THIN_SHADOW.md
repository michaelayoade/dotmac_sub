# Sub Thin Shadow

A disposable, egress-denied, synthetic-data environment for rebuilding and
exercising Sub as independently owned domain slices.

It is **not** a clone of Sub production and must never become a second live
financial authority. It satisfies **no** production cutover gate.

## What exists

| Thing | Value |
|---|---|
| Deployment directory | `/opt/dotmac-sub-thin-shadow` |
| Compose project | `dotmac-sub-thin-shadow` |
| Application bind | `127.0.0.1:18001` (loopback only, no nginx route) |
| Networks | `…_internal` (`internal: true`, holds all state) and `…_edge` (carries only the loopback publish; masquerade disabled) |
| Volumes | `…_db_data`, `…_redis_data`, `…_uploads`, `…_sink` |
| Services | `postgres`, `redis`, `migrate` (one-shot), `app` |
| App image | `ghcr.io/michaelayoade/dotmac_sub@sha256:342a9b80…` |
| App revision | `9a5db5de005e82241e2490a930d84e5a0566d3ff` (run `32216213910`) |

### Why two networks

Docker **silently ignores port publishing** for a container whose every network
is `internal: true`: the mapping is accepted, `docker ps` shows it empty, and
the bind looks configured right up until something tries to connect. So
everything holding state sits on the internal network with no route off the
host, and the app additionally joins a thin `…_edge` bridge that exists only to
make the loopback publish real. The edge denies egress a different way — IP
masquerade is off, so a packet leaving the container keeps its RFC1918 source
and is unroutable past this host, while inbound DNAT still works. The bridge is
also pinned to `127.0.0.1`, so a `ports:` entry that forgot its bind address
still cannot land on a public interface.

External **name resolution** is disabled too, on the services running the Sub
image: the embedded resolver's forwarder points at the container's own
loopback, where nothing listens. Denying TCP egress alone is not enough — the
first deployment refused every outbound connection and still resolved
`github.com`, which is a usable channel for anything willing to encode data in
a query. Service names still resolve, because Docker's embedded resolver
answers those itself.

No Celery workers, no Beat, no provider credentials, no OpenBao token, no access
to production Sub / Vendor CP / ERP / Integrator databases, no Docker socket, no
WireGuard, no host PID namespace, no privileged mode, no added capabilities.

Credentials are generated on the host into a mode-0600, untracked `.env`. They
are never printed and never committed.

## Where the rules live

The rules are executable, not prose:

- `app/shadow/` — the typed cohort/cutover manifest. Closed Pydantic models, a
  closed vocabulary, and validators that refuse a claim running ahead of its
  evidence.
- `tests/architecture/test_shadow_cohort_manifest.py` — manifest canaries.
- `tests/architecture/test_shadow_compose_contract.py` — compose canaries.
- `tests/architecture/test_shadow_boundary.py` — boundary canaries.

Every rejection canary is paired with a sensitivity proof, because a guard that
only ever sees conforming input passes even when it is broken.

## The honest state today

**Every module has `authority_mode = none`. Subscriptions, Billing and
Collections are `released_uncomposed`; the other 22 members are
`source_only`.**

That is the accurate reading, not an unfinished draft:

- `dotmac-subscriptions==0.1.0a3`, `dotmac-billing==0.1.0a1` and
  `dotmac-collections==0.1.0a1` are published. Their `ReleaseIdentity` values
  pin the exact wheel SHA256 values in Sub's checked-in Poetry lock, and their
  source revisions are the peeled annotated Starter release tags recorded in
  `docs/PLATFORM_ADOPTION_LEDGER.md`.
- The Thin Shadow app still runs the **pinned Sub baseline image**, which
  contains none of the cohort packages, and the stack mounts no host paths — so
  none of the published wheels is installed or executing in this environment.
- Publication is not composition. Calling these releases `source_only` would
  erase real publication evidence; calling them `installed_shadow` would claim
  a runtime the pinned image cannot contain.

`ModuleEntry` refuses each step that runs ahead of its evidence, so these states
can only move when the evidence does.

## Adoption sequence (intended order, inside shadow only)

1. Kernel prerequisites, tenant scope, idempotency, outbox, Durable Timers.
2. Sales → Orders → Subscriptions → Billing → Collections.
3. Sub invoice, settlement and allocation authority under **one coupled shadow
   watermark**.
4. Projects → Work Orders → Surveys.
5. Inbox → Campaigns.
6. Network suite, by explicit owner boundaries.
7. Analytics / Web Analytics — compatibility only.

Production constraints that shadow does **not** relax: Billing and Subscriptions
require Vendor CP platform adoption first; Analytics remains ERP-first; Web
Analytics remains Backoffice-first; Positioning's production adoption hold
stands. Each is recorded as a typed `BlockingPrerequisite` on its module.

## Deploying

The target host also runs live Keycloak. The script treats the host as something
it is a guest on:

```sh
sudo install -d -m 0755 /opt/dotmac-sub-thin-shadow
sudo cp deploy/shadow/docker-compose.shadow.yml /opt/dotmac-sub-thin-shadow/
sudo cp deploy/shadow/deploy-shadow.sh /opt/dotmac-sub-thin-shadow/
sudo bash /opt/dotmac-sub-thin-shadow/deploy-shadow.sh
```

It captures Keycloak's container IDs, images, creation times and health before
touching anything, compares them afterwards, and on **any** difference removes
only the shadow application container and fails. It verifies the pinned digests
are already present rather than pulling, proves egress is refused from inside
the container, and never removes a volume.

Do not change `/opt/keycloak`, its compose project, containers, network, volumes
or database; nginx; ports 80/443 or loopback 8080; firewall rules; or the backup
and restore controls.

## Rollback

```sh
cd /opt/dotmac-sub-thin-shadow
docker compose -p dotmac-sub-thin-shadow -f docker-compose.shadow.yml down
```

`down` **without** `-v`. Shadow volumes are preserved deliberately: destroying
them is a separate act that needs explicit authorization. Keycloak is untouched
by this command because it is a different compose project.

To roll back only the application while keeping the database for inspection:

```sh
docker rm -f dotmac_sub_thin_shadow_app
```

## The other cohort: `receivable-shadow-01`

Two different things in this repository are called a cohort, and conflating them
would misread both.

**This document's cohort is a module-adoption cohort** — twenty-five packages
and how far each has actually travelled. The three released commercial owners
are `released_uncomposed`; the other 22 entries are `source_only`; every entry
has `authority_mode = none`. `ModuleEntry` refuses each step that runs ahead of
its evidence.

**`receivable-shadow-01` is a data cohort** — which *rows* across
Subscription → Billing → Collections are projected into
`billing_receivable_projections` and compared by the seven-dimension parity
report. It
is declared in `app/services/billing/receivable_cohort.py` and designed in
`docs/designs/RECEIVABLE_PROJECTION_SHADOW.md`.

Neither implies the other:

* the module cohort answers "could `dotmac-billing` one day own this?";
* the data cohort answers "which facts would be compared if it ever could".

Recording a data cohort **does not** advance a module one step along
`ADOPTION_PROGRESSION`, and nothing in the projection package writes to the
manifest in `app/shadow/`. Both declarations read the immutable Subscriptions
release coordinates from `app/module_release_contracts.py`, so production code
does not import the shadow package. The a3 treatment contract is composed but
has no admitted runtime reader or mapping yet.
`test_the_standing_blocker_carries_real_pin_coordinates` fails the build if the
recorded blocker and adoption manifest ever disagree.

The data cohort also has a distinct sealed read-only readiness verdict. Run the
`readiness` subcommand from `scripts.billing.receivable_projection` with the
same exact code revision, schema revision and observation window used for
parity. It exits non-zero when the report seal or aggregate accounting does not
reproduce, or on an empty comparison, unresolved classification, projection
work still required, semantic divergence, any `not_expressible` result or a
standing contract blocker. There is no `--apply` form. A zero exit makes the
evidence eligible for a separately authorised authority review; it does not
move a writer, select a commercial provider, retire a fallback or authorise
deployment.

The data cohort also runs against **live Sub**, not inside the shadow stack:
it observes incumbent invoices and writes a rebuildable projection beside them.
It moves no authority, creates no collections case, and every writing command
defaults to dry run. That is a different kind of safety from this environment's
egress denial, and neither substitutes for the other.
