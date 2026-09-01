# Runbook — legacy image-pin bootstrap for `postgres-local`

One-time operation. Decision record: `docs/adr/0015-legacy-image-pin-bootstrap.md`.
Change reference: `CHG-SUB-9001-CONTAINMENT-2026-09-01`.
Host: `root@94.72.107.76` (`dotmac-sub-prod`). Service: `postgres-local`. Port: 9001.

## What this does, and the one thing it must not do

`postgres-local` publishes 9001 on both `0.0.0.0` and `[::]`. The v4 listener is
source-restricted to the replication standby `75.119.157.91/32` by a
`DOCKER-USER` rule; the v6 listener is governed by nothing. Correcting that
needs a recreate, and the steady-state reconcile refuses to recreate a service
whose image is a mutable tag.

This carries the service from `postgis/postgis:16-3.4-alpine` to the immutable
digest of **the bytes already running**, and corrects the listener in the same
recreate. It must never adopt whatever digest the tag points at *now* — that
could be a newer image, and the window would silently become an upgrade.

## Preconditions (all of them, before the window is scheduled)

1. **The deployed Compose file must carry the bind knob.** As measured on
   2026-09-01, `/root/dotmac_sub/docker-compose.yml` publishes a bare
   `- 9001:5432`. `main` publishes `${PG_LOCAL_BIND:-127.0.0.1:}9001:5432`.
   Until the host runs the release that carries the knob, setting
   `PG_LOCAL_BIND` does nothing and PLAN will refuse. Fix with an ordinary
   deploy; this is not part of the bootstrap.
2. Replication is streaming from `75.119.157.91`.
3. `vars.LEGACY_IMAGE_PIN_BOOTSTRAP_ENABLED` is `true`.
4. The root-owned observer config exists at
   `/etc/dotmac/legacy-image-pin-observer.json`, canonical, mode `0600`, schema
   `LegacyImagePinObserverConfigV1`, naming `dotmac-sub-prod`, project
   `dotmac_sub`, service `postgres-local` and the legacy tag.
5. `scripts/legacy_image_pin_observer.py` is installed byte-identically at
   `/usr/local/libexec/dotmac-legacy-image-pin-observer`, root-owned, `0750`.
6. No file at `/var/lib/dotmac/legacy-image-pin/receipt.json`. If one exists the
   bootstrap has already run and will refuse; that is correct, not a fault.

## Sequence

### 1. Two plans

Dispatch **Plan legacy image-pin bootstrap** from `main` twice, as two separate
first-attempt runs. Each is read-only: shared deploy lock, no Docker-socket
write, artifacts written outside the deployment directory.

Both `plan.json` files must be **byte-identical**. If they are not, the host
changed between them — stop and investigate rather than re-running until two
happen to agree.

### 2. Review

Read `plan.json` and confirm by eye:

* `legacy_image_reference` is the tag actually running.
* `desired_image_reference` names the **same repository**, and
  `resolution.resolved_image_id` equals `observed_image_id`.
* `current_listeners` is the dual-family pair; `desired_listeners` is the single
  IPv4 entry.
* `non_target_containers` lists every other running container.
* `bind_knob` shows the two injections landing on `0.0.0.0` and `127.0.0.1`.

### 3. Apply

Dispatch **Apply authorized legacy image-pin bootstrap** with the source SHA,
the reviewed `plan_digest`, and both plan run IDs.

Before mutating, APPLY takes the exclusive deploy lock, re-observes the complete
prestate under it, requires byte identity with the admitted plan, proves
replication is streaming, proves the desired digest resolves locally to the
running image ID, builds a root-owned rollback bundle, and arms a persistent
systemd deadman with a five-minute deadline.

Its only mutation is: set `PG_LOCAL_BIND=0.0.0.0:`, add the digest-pinned image
overlay, and `up -d --no-deps --no-build --pull never --force-recreate
postgres-local`.

### 4. Success evidence

The deadman is disarmed only after every one of these:

* target container ID **changed**;
* target image ID **identical**;
* effective image reference is the exact desired digest;
* `[::]:9001` **absent**, `0.0.0.0:9001` **present**;
* `75.119.157.91/32` still connects and replication is back to `streaming`;
* firewall proof shows only the declared IPv4 client path;
* an unauthorized external vantage is **refused**, with a **positive control**
  from that same vantage proving the probe actually left the building;
* the service definition is unchanged apart from its image reference;
* **every** non-target container ID unchanged;
* no image pulled or built.

The sanitized receipt is written to
`/var/lib/dotmac/legacy-image-pin/receipt.json` and uploaded as the run's
artifact.

## If it rolls back

The deadman restores the listener preimage and **keeps** the digest pin, then
writes a `rolled_back` receipt. That receipt refuses another bootstrap, and it
should: the durable half is already done, so the ordinary v2 PLAN/APPLY lane can
now retry the listener correction on its own. Use
`docs/runbooks/PUBLISHED_PORT_RECONCILE.md` from there.

## What this never touches

PostgreSQL auth, TLS, credentials or data; any port other than 9001; any
`.env` key other than `PG_LOCAL_BIND`; any other service. FreeRADIUS is out of
scope and gets its own facility, digest, plans, proofs and window.
