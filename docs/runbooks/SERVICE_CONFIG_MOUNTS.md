# Release-shipped service config: mounts and applying changes

This runbook covers the configuration that `freeradius`, `vmagent` and
`promtail` read from `config/` in the deployment directory: how it is mounted,
how a change reaches the running process, and what to do by hand.

## Why the mounts look the way they do

A single-file bind mount pins the inode that existed when the container was
created. `git pull` (and `git checkout`) write a changed tracked file as a new
inode, so the running container keeps reading the old file. A SIGHUP re-reads
the same stale inode. Only restarting or recreating the container picks up the
change, because Docker re-resolves every bind-mount source path when a
container starts.

On dotmac_erp this left vmagent on production and staging running a
two-week-old config with a literal `${DEPLOY_ENV}` environment label, so the
two environments' metrics were merged into one series
([dotmac_erp PR #695](https://github.com/michaelayoade/dotmac_erp/pull/695)).
Sub had the same mounts.

| Service | Mount | Container reads |
|---|---|---|
| vmagent | `./config/vmagent:/etc/vmagent:ro` (directory) | `-promscrape.config=/etc/vmagent/config.yml` |
| promtail | `./config/promtail:/etc/promtail:ro` (directory) | `-config.file=/etc/promtail/promtail-config.yml` |
| freeradius | seven single-file overlays, all `:ro` | the image's stock tree plus the overlays |

Each mounted directory holds only its agent's config file. Every file in a
mounted directory is readable by the container, so
`tests/architecture/test_config_directory_mounts.py` pins the directory
contents. A new file there must be a deliberate, reviewed addition.

### Why FreeRADIUS keeps single-file mounts

`freeradius/freeradius-server:3.2.7` ships a complete raddb tree at
`/etc/freeradius` (`/etc/raddb` is a symlink to it). The repository overrides
seven files in that tree: `radiusd.conf`, `mods-enabled/sql`,
`mods-enabled/sql_admin`, `sites-enabled/default`, `sites-enabled/admin-login`,
`dictionary` and `dictionary.mikrotik`. The other stock files are still
required: `mods-available`, `mods-config`, `policy.d`, `certs`, `clients.conf`,
`proxy.conf`, the stock `mods-enabled` modules and the `inner-tunnel` server.
A directory mount over `/etc/freeradius`, `mods-enabled` or `sites-enabled`
would hide them. Moving the overrides to a dedicated directory would also need
`radiusd.conf` include and startup changes. Those cannot be proven without
running the real image, and FreeRADIUS is subscriber authentication.

The single-file mounts are also safe here. The vmagent failure happened
because nothing restarted the container. FreeRADIUS reads these files only at
start, a HUP does not reload them (see below), and the restart that loads them
re-resolves every source path. The deploy now performs that restart, after
validating the new config. These seven mounts are the only allow-listed
single-file mounts of checkout config, and the test pins their exact container
paths.

`dictionary` and `dictionary.mikrotik` were writable mounts since the initial
commit. Nothing writes them: the app's pyrad dictionary path is a setting read
inside the app's own container. They are now `:ro` like the others.

### File permissions

Bind mounts keep the host file's mode and owner, and `:ro` does not change
them. FreeRADIUS refuses to start when its configuration is globally writable
("Refusing to start due to insecure configuration"). It reads the files as
root before dropping to `freerad`, so world-readable `0644` is correct. Git
creates tracked files as `0644` under the usual `022` umask. A checkout made
under a permissive umask (for example `000`) would produce `0666` files that
FreeRADIUS rejects. The deploy's `freeradius -XC` validation runs against the
same mounts and catches this before the running server is touched. The fix is
`chmod go-w` on the files in `config/freeradius`.

## How a change is applied by a deploy

`scripts/deploy.sh` never recreates these services (see `APP_SERVICES`). For a
config-file change, it restarts them:

1. For each of `vmagent`, `promtail` and `freeradius` that is declared and
   running, the deploy reads the container's start time and bind-mount sources
   (`docker inspect`). `scripts/deploy_config_freshness.py` reports any source
   inside the deployment directory whose files changed (inode change time)
   after the container started. Host paths such as `/var/run/docker.sock` and
   `/var/lib/docker/containers` are ignored.
2. A service with no changed source is left alone. A stopped service is never
   started.
3. `vmagent` and `promtail` get `docker compose restart <service>`. A failed
   restart is a warning. An observability agent never fails a healthy
   application deploy.
4. `freeradius` is validated, restarted, and checked (next section).

The comparison is between "what is on disk now" and "when the process
started", not between two release revisions. Both deploy hosts need that:

- **Staging.** The deployment directory is the release checkout. The staging
  workflow checks it out at the candidate before `deploy.sh` runs, so a release
  that changes `config/` is applied by that deploy.
- **Production.** The release Compose file comes from the Actions checkout, but
  `config/` comes from the persistent host directory (`/root/dotmac_sub`),
  which a deploy does not move (see `PRODUCTION_DEPLOYMENT.md`). A
  release-to-release diff would restart services whose mounted files never
  changed. A config change reaches production when the host checkout is
  updated. The next deploy then applies it. To apply it without a deploy,
  restart by hand (see below).

The check needs no recorded state. If a deploy fails before the apply step,
the next deploy still sees the change. A false positive (a file rewritten with
identical content, a `chmod`) costs one unnecessary restart. It cannot hide a
change.

## FreeRADIUS: validate, restart, verify

- **Why not HUP.** On SIGHUP (or `radmin -e hup`), FreeRADIUS 3 re-reads only
  HUP-safe module configuration. It does not reload `radiusd.conf`, virtual
  servers (`sites-enabled`), clients (including `read_clients` from the `nas`
  table) or dictionaries, which are the files shipped here. A HUP would also
  re-read the pinned single-file inodes. A restart is the least disruptive
  mechanism that applies these files.
- **Validate first.** When the freeradius config changed, the deploy runs
  `freeradius -XC` in a throwaway container:
  `docker compose run --rm --no-deps -T freeradius freeradius -XC`. That
  container uses the same image, environment and mounts, and publishes no
  ports. Validation runs twice:
  - before any database work, so a rejected config refuses the whole deploy
    while nothing has been touched;
  - immediately before the restart.

  A rejected config is never restarted into. The running server keeps the
  config it loaded. `-X` output masks secret-typed values, and only the last
  40 lines are printed on failure. Validation loads modules the way a start
  does, so it may also fail when the RADIUS database is unreachable. That
  refusal is deliberate, because a restart would fail the same way.
- **Disruption.** The restart is a stop and start of the existing container,
  normally a few seconds. Authentication and accounting pause for that time.
  NAS clients retransmit across it. The service definition is unchanged, so
  ports, image and volumes stay as they are. ADR-0014's published-port path
  still owns those.
- **Verify.** The container must stay `running` with no crash-loop restarts
  for `FREERADIUS_STABILITY_SECONDS` (default 5). Then the synthetic probe
  (`app.services.radius_probe.run_configured_probe`, run in `celery-worker`)
  must answer at least as well as it did just before the restart:
  - probe accepted before: it must be accepted after;
  - probe rejected before: it must still answer;
  - probe unconfigured or not answering before: only liveness is proven, and
    the deploy prints a warning.

  The timeout is `FREERADIUS_RESTART_TIMEOUT_SECONDS` (default 60).
- **If verification fails.** The application release is already accepted and
  is not rolled back. The deploy exits non-zero and prints
  `FREERADIUS RESTART HEALTH FAILED`. Treat it as a subscriber-auth incident:
  check `docker compose logs freeradius` and run
  `docker compose run --rm --no-deps freeradius freeradius -X`. The old config
  is no longer on disk, so there is no automatic config rollback. Revert the
  config commit in the checkout, validate, and restart.

Validation does not protect against one case. If the running container
restarts for another reason (a crash or a host reboot) after a bad config is
checked out, it loads that config unvalidated. Keep the deployment checkout at
validated revisions.

## Applying a change by hand

Use this only when a config change must take effect without a deploy, for
example on production after updating `/root/dotmac_sub`. On staging, add
`-p dotmac_sub` to every command: the staging compose project is
`dotmac_sub`, not the directory name.

```bash
# vmagent / promtail
docker compose restart vmagent        # or: promtail

# freeradius: validate, then restart, then check
docker compose run --rm --no-deps -T freeradius freeradius -XC
docker compose restart freeradius
docker compose ps freeradius
```

## One-time rollout of the directory mounts

The mount change is a change to the service definition. A restart keeps the
old definition, and a deploy does not recreate these services, so each host
needs one recreate. Until then the containers keep their old single-file
mounts. Config changes are still applied, because a restart re-resolves those
paths too.

Only recreate a service that is already running. `up -d` starts a stopped
service. Check first with `docker compose ps vmagent promtail`.

Staging (`/home/dotmac/deploy-worktrees/dotmac-sub-staging`), after a staging
deploy of a release that contains this change:

```bash
cd /home/dotmac/deploy-worktrees/dotmac-sub-staging
docker compose -p dotmac_sub ps vmagent promtail
docker compose -p dotmac_sub up -d --no-deps vmagent promtail
docker inspect dotmac_vmagent dotmac_sub_promtail \
  --format '{{.Name}}: {{range .Mounts}}{{.Source}} -> {{.Destination}} {{end}}'
```

Production (`/root/dotmac_sub`), once its checkout is at a `main` revision that
contains this change:

```bash
cd /root/dotmac_sub
docker compose ps vmagent promtail
docker compose up -d --no-deps vmagent promtail
docker inspect dotmac_vmagent dotmac_sub_promtail \
  --format '{{.Name}}: {{range .Mounts}}{{.Source}} -> {{.Destination}} {{end}}'
```

The mounts should show `.../config/vmagent -> /etc/vmagent` and
`.../config/promtail -> /etc/promtail`.

The freeradius `:ro` change on the two dictionaries also needs a recreate to
take effect. It is hardening, not a functional change. Schedule it like any
FreeRADIUS maintenance, because the recreate pauses authentication for a few
seconds. A recreate applies the whole current service definition, published
ports included. Confirm the resolved ports match what is running before
recreating (`docker compose config freeradius`; see ADR-0014). Validate first:

```bash
docker compose run --rm --no-deps -T freeradius freeradius -XC
docker compose up -d --no-deps freeradius
```

On staging, add `-p dotmac_sub` to both commands.
