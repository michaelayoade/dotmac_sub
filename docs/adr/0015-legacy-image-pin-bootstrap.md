# ADR 0015 — A structurally single-use bootstrap out of a legacy image tag

Status: accepted (2026-09-01)
Change reference: CHG-SUB-9001-CONTAINMENT-2026-09-01
Supersedes nothing. Amends ADR 0014 in one place: the observation contract.

## Context

ADR 0014 declared published-port intent and the v2 reconcile enforces it. The
v2 lane recreates exactly one service and, to make that safe, requires the
service's image to be an immutable `name@sha256:…` reference. That requirement
is correct: a mutable tag can resolve to different bytes between the moment a
plan is made and the moment a container is recreated, so a recreate through a
tag is an unreviewed upgrade wearing a containment change's clothes.

Two things were measured on `dotmac-sub-prod` (`root@94.72.107.76`) on
2026-09-01 that the v2 design did not account for.

**1. The digest requirement was aimed at the wrong containers.** The observer
demanded an immutable reference from EVERY container in the Compose project.
Nine of the twenty-two non-target containers are ordinarily tag-pinned —
`redis-local`, `genieacs-mongodb`, `nominatim`, `victoriametrics`, `vmagent`,
`promtail`, `genieacs`, `freeradius`, `radius-db`. Pinning `postgres-local`
alone would still have failed PLAN.

**2. `postgres-local` cannot take its own first step.** It runs
`postgis/postgis:16-3.4-alpine`, a mutable tag, and publishes 9001 on both
`0.0.0.0` and `[::]`. The v4 side is source-restricted to the replication
standby `75.119.157.91/32` by a `DOCKER-USER` rule; the v6 side is governed by
nothing, because its traffic terminates on `INPUT` rather than traversing that
chain. Correcting the listener requires a recreate, and v2 refuses to recreate
a tag-pinned service. The containment fix is locked behind the very rule that
makes it safe.

## Decision

### 1. Split the observation contract along the line that matters

Immutable image identity is required of the **target** — the container this
operation destroys and recreates — and of nothing else.

A **non-target** is never recreated, so its provenance is not a property this
operation can promise anything about. What must hold is that it is the SAME
RUNNING CONTAINER afterwards, and a container ID proves that strictly better
than an image reference does. Non-targets therefore carry service, container
and container ID only.

The tag is not merely tolerated: `PublishedPortProjectContainerV1` has no image
field, so a non-target's provenance is unrepresentable and cannot be borrowed
as evidence about the target either.

The steady-state rule is undiminished. A tagged TARGET is still refused, and a
mutable tag plus an image ID is still not admissible PLAN evidence.

### 2. A separate, single-use bootstrap carries the service across

`LegacyImagePinBootstrapPlanV1` and its lanes are a DIFFERENT schema family
from the steady-state contracts, which is what keeps the tag admissible here
and nowhere else: a bootstrap snapshot cannot be handed to the v2 planner,
because the strict contracts refuse each other's bytes.

The digest is taken from the RUNNING image's own registry digest, keyed by the
running image ID, and must be proved to resolve locally — no pull, no build —
to that same image ID. It is never obtained by asking a registry what the
mutable tag means now: that could name a newer image, and adopting it would
smuggle an upgrade into a containment change. **If the running bytes cannot be
bound to a registry digest, the bootstrap stops.**

### 3. Single-use is a mechanism, not a request

Three independent refusals:

* The bootstrap snapshot admits only the exact legacy prestate — a mutable tag
  and the dual-family publish. After the bootstrap the target carries a digest
  and one IPv4 listener, so a second attempt cannot even be *described*.
* A root-owned terminal receipt at `/var/lib/dotmac/legacy-image-pin/receipt.json`
  is checked by the observer, by PLAN, by ADMISSION and by the host adapter on
  both lanes. An unreadable receipt refuses too: corrupting the file must not
  re-enable the operation.
* A **rollback** writes that receipt as well. A rolled-back bootstrap has still
  achieved its durable half — the immutable reference is retained — so ordinary
  v2 PLAN/APPLY can now own the listener correction on its own, and repeating
  the bootstrap would only buy a second unreviewed recreate.

### 4. Rollback keeps the pin

The bootstrap deadman restores the LISTENER preimage but retains the digest
reference. The bytes are identical either way — the digest names the image that
was already running — and reverting to the tag would put the service back in
the state the steady-state lane refuses to touch, which is the exact condition
this bootstrap exists to remove.

### 5. The bind variable is proved, not assumed

Measured on the host: the DEPLOYED `docker-compose.yml` publishes a bare
`- 9001:5432`, with no `${PG_LOCAL_BIND}` interpolation at all. `main` carries
the knob; the host is running an older release that does not. Against that
file, setting `PG_LOCAL_BIND=0.0.0.0:` changes nothing — the recreate would
faithfully reproduce the dual-family publish, the defect would survive the
window, and only the deadman would notice.

So the observer renders the effective projection under two different injected
values and refuses unless both land where predicted. One probe would not be
enough: a file that hardcoded the wildcard would satisfy a single wildcard
probe while being just as unresponsive to the variable. The loopback probe is
the control that makes the first result mean anything.

**Operational consequence: the bootstrap cannot run until the host's deployed
Compose file is the release that carries the knob.** That is an ordinary
deploy and is a precondition of the maintenance window, not part of this change.

## Scope

Only `postgres-local`, only port 9001. PostgreSQL auth, TLS, credentials, data,
other ports, `.env` beyond the one declared bind variable, and every other
service are out of scope. FreeRADIUS gets the same generic facility later, with
its own digest, plans, proofs, receipt and window; the two are deliberately not
combined.

## Consequences

* One extra observer, owner, deadman and pair of workflows exist for a single
  operation, and are spent afterwards. That is the cost of not weakening the
  steady-state rule to accommodate a one-time transition.
* After the bootstrap, `postgres-local` is an ordinary v2 subject.
* The facility generalises: FreeRADIUS's later bootstrap should reuse this
  shape rather than relax v2.
