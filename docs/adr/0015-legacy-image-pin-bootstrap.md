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

### 4. Recovery goes FORWARD, and keeps the pin

Michael ruled roll-forward on 2026-09-01, and the ruling inverts what an
earlier draft asserted:

> **The deadman must not restore the dual-family listener. That listener is the
> vulnerability, not a healthy rollback state. A dual-family listener
> reappearing is a deadman FAILURE.**

So automatic recovery recreates forward: the retained immutable pin, the
IPv4-only bind, and an explicit refusal if any IPv6 listener is observed
afterwards. There is deliberately no `before_listeners` field in the deadman
state — a value that must never be restored should not sit where it can be
mistaken for a target — and the deadman executable no longer carries the
vocabulary for going backwards at all.

Recovery succeeds only on all of: PostgreSQL healthy · standby
`75.119.157.91` streaming · exactly one IPv4 listener · **no IPv6 listener** ·
pinned image unchanged · **data/volume identity unchanged** · every non-target
container ID unchanged.

Volume identity is a fingerprint of the target's mounts (type, name, source,
destination, rw). A recreate that preserved the container-ID discipline and the
image but silently re-bound a volume would pass every other check, and the
thing it moved would be the data.

Returning to the dual-family publish is **break-glass**: separately authorized,
never automatic, and never retained on disk for convenience. The pre-staging
Compose is therefore not bundled anywhere.

### 4b. Two inputs, not one: CURRENT is revalidated, DESIRED is pinned

The plan binds two separate inputs, and they are not interchangeable.

**CURRENT** is what the host actually has: the digests of its DEPLOYED Compose
files, the live target container, and every non-target container identity. The
host's deployed tree is the sole authority for this. An Actions checkout must
never masquerade as observed production state.

**DESIRED** is the bytes APPLY will use: an immutable release Compose digest and
a fully determined overlay digest. These are pinned in the plan, so APPLY uses
exactly the reviewed bytes rather than whatever a checkout happens to contain
when it runs.

The plan requires the desired release bytes to be **among** the deployed bytes.
That single validator is the staging precondition made structural: until the
host carries this exact release, a plan cannot be constructed at all, so
"verify the deployed Compose digest" stops being a step somebody performs and
becomes one the contract will not let anybody skip. It also resolves the
tension both ways — applying a checkout would diverge observation from
execution, while applying the host's file alone would leave the change with no
immutable definition of what it is applying.

APPLY revalidates CURRENT under the lock and refuses outright — not warns — if
the deployed bytes have moved, because **staging the release that carries
`PG_LOCAL_BIND` moves them**. No plan taken before staging survives it. The
deployed digests are also folded into the prestate key, so a moved host is
refused twice over; the dedicated check runs first only so the operator is told
*which* coordinate moved.

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

**Operational consequence — the authorized sequence.** The bootstrap cannot run
until the host's deployed Compose is the release carrying the knob:

1. Publish and verify an exact Sub release carrying the knob.
2. Authorize staging that release's Compose on the host **without recreating
   `postgres-local`**.
3. Verify the deployed Compose digest.
4. Take **fresh**, byte-identical bootstrap plans.
5. Apply the digest pin and IPv4-only binding in a separately named window.
6. Verify non-target container IDs, replication, listener families and firewall
   reach.
7. Produce the bootstrap receipt, then demonstrate steady-state v2 admission.

Step 4's *fresh* is load-bearing and is enforced, not advised: staging rewrites
the deployed Compose, so any earlier plan's CURRENT input no longer describes
the host and is refused at apply.

If staging cannot avoid recreating PostgreSQL, then staging and containment
become **one explicitly authorized maintenance operation**, and no plan taken
before staging survives it.

Step 7 is also what discharges the outstanding ADR 0034 obligation in § 6: the
steady-state real-target admit becomes demonstrable only after the bootstrap has
executed.

## 7. Can staging avoid recreating `postgres-local`? Yes — measured

Step 2 of the sequence asks for the release's Compose to be staged *without*
recreating `postgres-local`. Measured on `dotmac-sub-prod` 2026-09-01, it can be:

* **Nothing depends on it.** No service in the deployed base Compose, the host
  override, or the release Compose declares `depends_on: postgres-local`. The
  only `depends_on` edges in the project are `victoriametrics` (three workers
  plus `vmagent`) and `genieacs-mongodb` (`genieacs`).
* **No deploy names it.** `deploy.sh` runs `up -d "${APP_SERVICES[@]}"`, and
  `APP_SERVICES` is `app`, the eight `celery-worker-*`, `celery-beat`,
  `bandwidth-poller` and `syslog-listener`. `postgres-local` is neither named
  nor in that set's dependency closure. `deploy.sh` touches the container only
  through `docker exec` for dumps and a `pg_dump` guard — never a recreate.
* **Staging is a file write.** Placing the release Compose recreates nothing on
  its own; only a `docker compose up` naming the service (or a bare
  `docker compose up` with no arguments, which names everything) would.

So staging and containment do NOT have to be one operation. Michael's fallback —
*"staging and containment become one explicitly authorized maintenance
operation; no plan taken before staging survives it"* — is not required here,
though the plan refuses a stale `current` input either way (§ 4b).

## 8. The hazard staging itself creates

The release publishes `${PG_LOCAL_BIND:-127.0.0.1:}9001:5432`, and
`PG_LOCAL_BIND` is **absent** from the production `.env` (measured: zero
occurrences). Rendering the release Compose against that `.env`:

| `PG_LOCAL_BIND` | resolved publish | admits `75.119.157.91/32`? |
|---|---|---|
| unset | `127.0.0.1:9001:5432` | **no** |
| `0.0.0.0:` | `0.0.0.0:9001:5432` | yes |

The instant the release Compose is staged, the deployed definition resolves to
**loopback**. The running container is untouched, so nothing breaks
immediately — but the next recreate of `postgres-local`, by this operation or
by anything else, strands the replication standby on a port it is actively
streaming WAL through. The change written to remove an exposure would have
caused an outage.

**Therefore staging MUST set `PG_LOCAL_BIND=0.0.0.0:` in the same authorized
step that places the Compose file.** That makes staging bind-neutral: the
resolved publish equals the currently effective IPv4 listener, so an accidental
recreate reproduces today's exposure rather than severing replication.

This is refused at plan time rather than trusted: the observer now renders the
effective projection a third time with the host's REAL environment and no
injection, and the contract refuses any host whose current resolved bind does
not admit the standby.

## 9. The commit point — the recovery contract

Staging lands two things that are only safe together, and two files cannot be
renamed in one atomic step. A journal supplies the atomicity, and its state is
the named boundary between two regimes:

**Before the commit point.** Nothing is committed. Both originals are
preserved, and `recover` restores them atomically, leaving the host exactly as
it was observed. This is what a torn write must leave behind — never the
half-applied pairing, which is precisely the state (release Compose, no
`PG_LOCAL_BIND`) where the next recreate strands the standby.

**After the commit point.** Recovery never goes backwards. It recreates forward
with the retained pin and the IPv4-only bind. The preserved originals are
deleted at the commit point, so the way back is destroyed at exactly the moment
it must never be taken.

The boundary is a single journal write, so it is explicit rather than implied
by where an exception happens to be raised.

## 9b. Superseded — what a rollback could restore after staging

Once the release Compose is deployed, the bare dual-family publish **no longer
exists in any file on the host**, so a rollback cannot reproduce it. The
deadman's preimage assertion (restore the observed dual-family listeners) is
therefore unsatisfiable after staging, and every rollback would fail its own
check.

Two possible resolutions, and this one is **not** ours to pick:

1. **Roll forward.** Accept that rollback restores the single IPv4 listener and
   the retained pin. The bytes are unchanged and the standby keeps its path;
   the dual-family state is the defect, so declining to recreate it is
   arguably correct. But it contradicts "so ordinary v2 PLAN/APPLY can safely
   retry the listener correction" — there would be nothing left to correct.
2. **Bundle the pre-staging Compose** so the dual-family publish is genuinely
   restorable, preserving the stated rollback semantics at the cost of keeping
   a known-vulnerable definition on disk specifically to be able to return to
   it.

Michael's ruling is required before the window. Until then the deadman still
asserts the dual-family preimage, which is correct for an unstaged host and
will fail loudly rather than silently on a staged one.

## Scope

Only `postgres-local`, only port 9001. PostgreSQL auth, TLS, credentials, data,
other ports, `.env` beyond the one declared bind variable, and every other
service are out of scope. FreeRADIUS gets the same generic facility later, with
its own digest, plans, proofs, receipt and window; the two are deliberately not
combined.

## 6. Admit demonstrations, and one outstanding obligation

A gate that enumerates real targets has to be shown admitting one of them. This
observer is the case that made that concrete: it demanded an immutable
reference from every container in the project, so it could never have admitted
anything it would actually be asked to admit — and nothing noticed, because
acceptance had only ever been exercised against hand-built inputs and refusal
only against planted ones. That pair misses this defect by construction.

`tests/architecture/test_declared_target_admission.py` therefore draws its
demonstrations from DECLARED state: the target enumeration comes from each PLAN
workflow's own `options:` list rather than a copy kept in the test, and the
image under test is the one `docker-compose.yml` really declares.

`deploy/shadow/docker-compose.shadow.yml` is deliberately not a source. It is
digest-pinned throughout and even carries the exact PostGIS digest this
bootstrap adopts, but for a stack the PLAN workflow does not offer; an admit
drawn from it would be green and would mean nothing. The service names are
asserted disjoint so the two cannot be confused.

**Delivered now.** The bootstrap lane's admissibility property — the target
carries the exact configured legacy tag — is satisfied by real declared state
today, so its real-target admit is demonstrated against
`postgis/postgis:16-3.4-alpine` as `docker-compose.yml` declares it, alongside
a planted refusal through the same validator.

**Delivered now for the steady-state lane.** The real-target REFUSAL over the
live enumeration: every service that workflow offers is fed, as declared, to
the real immutable-image validator, and every one is refused. That is the
observation that would have caught the original defect. Both of that
validator's branches are also exercised for the first time — a planted admit
and a planted refusal — because a validator only ever seen refusing is
indistinguishable from one that refuses unconditionally.

**Owed.** The steady-state lane's real-target ADMIT. It is not writable yet:
both declared services are tag-pinned, which is the whole reason this bootstrap
exists. Writing a digest-pinned stand-in and calling it an admit would be
exactly the defect this section is written from, so it is recorded as an
obligation rather than fabricated.

The obligation discharges after the bootstrap has EXECUTED — not when this
change merges. Note precisely where the admissible target then appears: the
bootstrap applies a host-side Compose overlay and does not pin the release
Compose file, so the digest-pinned target exists in the host's effective
Compose, which CI cannot observe.

`test_a_declared_target_that_becomes_digest_pinned_must_retire_this_gap`
ratchets the half that IS repository-observable and fails the moment a declared
target becomes digest-pinned in `docker-compose.yml`. The host-side half is
discharged by executed evidence from the maintenance window and is tracked
here, not by a check, because a check that cannot observe a condition must not
claim to guard it.

## Consequences

* One extra observer, owner, deadman and pair of workflows exist for a single
  operation, and are spent afterwards. That is the cost of not weakening the
  steady-state rule to accommodate a one-time transition.
* After the bootstrap, `postgres-local` is an ordinary v2 subject.
* The facility generalises: FreeRADIUS's later bootstrap should reuse this
  shape rather than relax v2.
