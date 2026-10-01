# Production deployment

`scripts/deploy.sh` is the production deployment owner. It deploys one immutable
GHCR image and keeps the database, proxy handoff, application health, and
rollback boundary in one operation.

## Host contract

- `nginx/selfcare.dotmac.io.conf` is installed and `nginx -t` passes.
- The primary upstream is `127.0.0.1:8001`.
- The long-running backup app bind is `127.0.0.1:18001`; it is not the deploy warm-candidate route.
- The warm candidate upstream is `127.0.0.1:18002` by default. Do not reuse
  `18001`; that port is reserved for the long-running backup app.
- `.env` contains the production service configuration and approved secret
  references. Secret values are not copied into deployment commands or logs.
- `.env` identifies the exact production host with `APP_ENV=production` and
  `SERVER_NAME=dotmac-sub-prod`. The release gate rejects ambiguous markers.
- GitHub workflow evidence is readable from the host. Public repositories need
  no credential; restricted repositories inject the read-only
  `GITHUB_DEPLOY_GATE_TOKEN` through the approved secret-delivery path.
- Release and backup-policy verifiers import only from the exact authorized
  Actions checkout. The mutable deployment checkout is deliberately excluded
  from Python's safe path, so a stale or locally modified `scripts/` package
  cannot interpret release evidence or decide backup policy.
- The database backup and deploy locks are writable.
- `DATABASE_URL` is the non-superuser, NOBYPASSRLS `app_user` runtime
  connection after the reviewed ownership/grant cutover. The deploy reads it
  only from the deploy directory's `.env`; an inherited process
  `DATABASE_URL`, including an empty exported value, is refused before any
  Compose command because it would override Compose's `--env-file` value.
  The deploy process receives a distinct held `MIGRATION_DATABASE_URL` for an
  actual `app_admin` login (`BYPASSRLS`, `NOSUPERUSER`, `NOCREATEDB`,
  `NOCREATEROLE`, without database `CREATE`). Never persist that URL in `.env`.
  Compose masks it in long-running services and injects it only into one-shot
  migration/prerequisite containers. Missing or shared DSNs fail before repair.
- The module prerequisite repair leg has the dedicated
  `dotmac_schema_bootstrap` credential available as a root-owned `0400` pgpass
  file at `/etc/dotmac/sub/schema-bootstrap.pgpass`, with the passwordless
  `SCHEMA_BOOTSTRAP_URL` configured. Without it a deploy that needs repair is
  `blocked` and stops. An elevated `BOOTSTRAP_DATABASE_URL` is injected only
  for one-off operator provisioning; neither is an application connection
  string and neither is ever logged.
- Host-side release-control modules execute from the exact authorized Actions
  checkout through `scripts/run_repo_module.sh`. `PYTHONPATH` alone is not an
  admissible checkout boundary because Python searches the current deploy
  directory first; a stale `/root/dotmac_sub/scripts` package must never shadow
  the authorized verifier.
- Docker daemon access and exact-container inventory are readable by the
  production runner. An existing `dotmac_sub_app` container carries a valid
  full-SHA `org.opencontainers.image.revision` label.

The deployment refuses to start if the running Nginx configuration does not
contain the warm candidate upstream.

### Held migration connection for workflow deploys

The staging and production environments set the **non-secret** protected
variable `MIGRATION_DATABASE_URL_FILE` to an absolute, host-local path outside
the source checkout and deployment directory. A separately authorized host
materializer must provision the file before deployment: it must be a regular
file owned by the runner's effective user, exactly mode `0400`, and contain
one UTF-8 PostgreSQL `app_admin` URL of at most 8192 bytes with no newline.
Neither this repository nor its workflow creates or copies that credential.
The materializer and its authorization are host operations, not consequences
of merging this code.

`scripts/with_migration_connection.py` refuses a missing or conflicting
pointer, symlink, wrong owner/mode, source/deploy path, malformed URL, or an
already-set `MIGRATION_DATABASE_URL` or `DATABASE_URL`. It passes the URL only
in the child environment of the exact staging or production deploy adapter
via `exec`, never in arguments or logs. The deploy preflight still checks that the
URL differs from the runtime `DATABASE_URL` in `.env`; direct process-held
`MIGRATION_DATABASE_URL` remains available for separately authorized manual
operator use. The file path must not be placed in `.env`, and the credential
must not be copied to app or worker configuration. Long-running Compose
services keep `MIGRATION_DATABASE_URL` empty.

Before schema repair, backup, or Alembic, the deploy runs
`scripts/verify_database_connection_pair.py` in one short-lived container of
the exact candidate image. It receives the runtime URL read from `.env` as
`DATABASE_PAIR_RUNTIME_URL` and the held migration URL as environment values
only. Two read-only catalog sessions (10-second connection and statement
limits) prove actual `session_user` and `current_user` identities, the checked
in role postures, and the same `current_database()`, TCP server address/port,
and `pg_postmaster_start_time()`. DNS aliases may differ; incomplete or
different observed backend identities refuse the deploy. No application rows
or schema are read or changed by this proof, and connection errors report only
generic codes. The runtime URL is never passed in arguments or printed.

For an initial deployment, the PostgreSQL service must already be reachable
from the candidate image's Compose network, and the separate `app_user` and
`app_admin` logins must already be provisioned with their required posture.
The pair proof does not create a database or a role and cannot be bypassed by
the first-deployment authorization.

Before anything touches the host, `scripts/deploy_production.sh` verifies the
typed production authorization and observes the running revision. The gate is
the first step after argument validation, so it runs before the hotfix
migration-evidence collection as well as before `scripts/deploy.sh`, the
backup, and migrations; a refusal leaves production exactly as it found it.
Docker daemon, inventory, and container-inspection failures are distinct from
an empty host and all fail closed. A missing or malformed revision label on an
existing container also fails closed; it is not treated as a first deployment.

For a genuine first deployment, the daemon must be readable and
`dotmac_sub_app` must be confirmed absent. Supply all three protected workflow
inputs: `bootstrap_target_revision` (the exact staged full SHA),
`bootstrap_change_reference`, and `bootstrap_reason`. The workflow writes a
typed authorization bound to `dotmac-sub-prod` and that exact revision. It is
refused if the container exists, if any input is partial, or if rollback inputs
are also present. Bootstrap cannot be combined with hotfix or post-migration
resume modes.

## Release sequence

1. Resolve the base Compose contract from the exact authorized release
   checkout, while resolving `.env` and any host-specific override from the
   persistent deployment directory.
2. Pull the image, verify its OCI revision matches the requested SHA tag, and
   require its `io.dotmac.release.source-tree` label to match the authorized
   release checkout's Git tree. A stale host Compose file cannot silently omit
   a service introduced by the image.
3. Require successful `CI` and `Mobile CI` GitHub push workflow runs for that
   exact full revision on `main`. Missing, pending, failed, wrong-branch, or
   unavailable evidence fails closed before backup or database mutation.
4. Verify the warm-candidate port is free. A port collision fails here before
   backup or migration.
5. Require the separate one-shot migration connection and prove both actual
   logins reach the same database backend. Run database prerequisite bootstrap
   if `BOOTSTRAP_DATABASE_URL` is supplied, then verify commercial module
   schemas and outbox dispatcher roles through the restricted migration
   connection. Missing prerequisites fail here before backup and Alembic.
6. Back up the database.
7. Run candidate-image pre-migration state checks against the target database.
8. Pin the immutable image and revision.
9. Apply `alembic upgrade heads`, retrying bounded PostgreSQL lock timeouts.
10. Verify registered schema contracts and reject every invalid or unready
   user-schema index.
11. Verify every enabled integration installation pin resolves to a current or
   bounded historical definition in the new image. Unavailable pins block
   replacement; historical pins are reported for explicit adoption.
12. Start and health-check the new application image on `127.0.0.1:18002`.
13. Recreate the primary application and workers. Nginx uses the healthy
   candidate while the primary port is unavailable.
14. Verify the primary image has no source-code bind mount and wait for its
   health endpoint.
15. Require every declared Celery worker to remain restart-free and answer a
   node-specific ping, and require Celery Beat to remain running without
   restarts, across a bounded stabilization window.
16. Gracefully drain the candidate and retain the configured rollback images.

The candidate runs the same image, environment, and database schema as the
primary. It is bound to localhost and exists only for the handoff window.

## Deployment retention

Production deployment backups are written to
`/var/backups/dotmac_sub/deployments`. Each filename retains the GitHub run ID
required by post-migration resume, while cleanup selects the stable
`dotmac_sub_run_` family across both that directory and the legacy
`/var/backups/dotmac_sub` location. The five newest deployment backups are
retained. Files outside that family are manual or migration evidence and are
never selected by deployment cleanup. Legacy deployment backups age out across
the next five successful deployments without moving a path that a failed-run
resume may still name.

Image cleanup identifies images by their full Docker image ID. Digest-pulled
images that Docker displays with a `<none>` tag remain eligible. Every image
referenced by any container is protected, and the five newest unused
application images are retained for rollback. Older unused application images
are removed by ID. When fewer than five rollback images exist, all are kept.
Do not substitute `docker image prune -a`: Docker protects running containers,
but that broad command does not preserve the required rollback history.

Both cleanup steps print the retained and removed counts and verify their
result. Backup cleanup failure stops before migrations. Image cleanup runs only
after the application and workers pass their health gates; a failure marks the
deployment run failed without rolling back the already healthy release.

## Module database prerequisites

Composed modules own one immutable `mod_*` schema each, and those schemas and
their cluster roles are privileged deployment prerequisites: Alembic runs as
the restricted migration role, which deliberately never holds database-level
`CREATE`, and only verifies that the prerequisites exist.

The schema set is not listed here. It is derived from the composed lineages in
[`../generated/MODULE_SCHEMA_CONTRACT.md`](../generated/MODULE_SCHEMA_CONTRACT.md),
regenerated by `make schema-contract` and drift-checked by
`make schema-contract-check`. Three hand-maintained prose copies of that list
previously existed and all three had missed `mod_inbox`, which is how it
reached production unprovisioned on 2026-08-31.

`scripts/deploy.sh` probes the contract with the restricted migration
connection before backup and before Alembic, and reports exactly one of three
outcomes:

- `already_satisfied` — the contract holds; nothing was written.
- `repaired` — the managed credential brought the database to contract.
- `blocked` — repair is required and cannot proceed. The deploy REFUSES and
  stops before migrations, naming the exact preflight check that failed.

`blocked` is the correction. The repair leg previously returned success
whenever no elevated credential was configured, so "nothing to do" and
"nothing can be done" were the same answer and the deploy carried on to a
verification it could not satisfy.

Repair on the deployment path uses a dedicated cluster role,
`dotmac_schema_bootstrap`: NOSUPERUSER, NOCREATEDB, NOCREATEROLE,
NOREPLICATION, NOBYPASSRLS, NOINHERIT, with `CONNECT` and `CREATE` on this
database only, and separately provisioned to act as `app_admin` so it can
create missing schemas with the approved `app_admin` owner. Its ability to
assume that role is elevated authority despite its own NOBYPASSRLS flag;
membership must be separately reviewed and is never granted by ordinary
deployment. It has no routine application or migration use; only the repair
leg connects as it. Existing schema-owner drift blocks every repair mode
before writes. An ownership transfer requires a separate reviewed plan.

OpenBao is the system of record for its production credential,
`secret/dotmac/postgres/sub-production-primary/schema-bootstrap`. The
deployment consumes already-held material and does not fetch OpenBao on the
deployment path: the credential is materialised on the host as a root-owned
`0400` pgpass file at `/etc/dotmac/sub/schema-bootstrap.pgpass`, readable only
by the deployment adapter's fixed account and by nothing else — not the
application container, not any other service account, not `app_admin`.

That account is `root` on production and `dotmac` on staging, set with
`SCHEMA_BOOTSTRAP_OWNER` (default `root`). Note what is deliberately NOT
claimed: the GitHub runner executes the deployment adapter on both hosts
(production runs the runner as `root`, staging as `dotmac`), so "the runner
cannot read this file" is unachievable in either environment and is not
asserted. Stating it would be an invariant that is quietly false everywhere,
which is worse than one scoped honestly.

Staging uses a separate role and a separate credential — sharing one would make
a staging compromise a production one and defeat the point of a narrowly scoped
role. Never write a credential value into this runbook, a deployment command, an
environment variable or a log.

The connection is TCP with SCRAM and carries no password: libpq reads it from
`PGPASSFILE` alone, so it appears in no URL, argv, environment or log. The
deploy refuses to attempt repair unless the URL is passwordless and the
credential file exists, is a regular file, is non-empty, is owned by
`SCHEMA_BOOTSTRAP_OWNER` (default `root`), and is mode `400`. Every one of those
checks names what failed; none of them is a bare `test`.

The address is the one the REPAIR LEG sees, not the one an operator sees. The
leg runs `docker compose run --rm --no-deps app`, so `127.0.0.1` there is the
container's own loopback, not the host's — a published `127.0.0.1:9001` cannot
be reached from inside it. Use the Compose service name the application already
resolves:

```bash
# production
SCHEMA_BOOTSTRAP_URL=postgresql://dotmac_schema_bootstrap@postgres-local:5432/dotmac_sub
SCHEMA_BOOTSTRAP_PGPASS=/etc/dotmac/sub/schema-bootstrap.pgpass

# staging (separate role, separate credential, adapter runs as dotmac)
SCHEMA_BOOTSTRAP_URL=postgresql://dotmac_schema_bootstrap@db:5432/dotmac_sub
SCHEMA_BOOTSTRAP_PGPASS=/home/dotmac/dotmac-sub-secrets/schema-bootstrap.pgpass
SCHEMA_BOOTSTRAP_OWNER=dotmac
```

This also makes the credential independent of the published `9001` binding, so
changing that binding cannot break the repair path.

libpq matches a pgpass line on the host string exactly as given in the URL, so
the file carries both perspectives — the container one used by the deploy and
the host one used by an operator. Two explicit lines, never a `*` host: a
wildcard would silently authorise the credential against any host it is ever
copied to.

```
postgres-local:5432:dotmac_sub:dotmac_schema_bootstrap:<value from OpenBao>
127.0.0.1:9001:dotmac_sub:dotmac_schema_bootstrap:<value from OpenBao>
```

The bootstrap has three modes. `--repair-schemas` is the deployment's mode: it
holds only `dotmac_schema_bootstrap`, so it creates missing schemas and repairs
schema grants only when it can act as `app_admin`. Existing ownership drift
requires a separate cutover. It never transfers ownership and,
being NOCREATEROLE, reports a missing or mis-postured cluster role as `blocked`
rather than working around it.

```bash
BOOTSTRAP_DATABASE_URL="$SCHEMA_BOOTSTRAP_URL" \
PGPASSFILE=/etc/dotmac/sub/schema-bootstrap.pgpass \
  python scripts/bootstrap_commercial_module_prereqs.py --repair-schemas
```

`--repair` remains the elevated one-off operator provisioning path, run out of
band once per environment, because it creates cluster roles as well as schemas.
Supplying `BOOTSTRAP_DATABASE_URL` to the deploy still selects it.

```bash
BOOTSTRAP_DATABASE_URL=postgresql://postgres@.../dotmac_sub \
  python scripts/bootstrap_commercial_module_prereqs.py --repair

BOOTSTRAP_DATABASE_URL=postgresql://postgres@.../dotmac_sub \
  python scripts/bootstrap_outbox_dispatcher_roles.py --repair
```

`--verify-only` is read-only through the restricted migration connection. The
deploy owner runs it before backup and before `alembic upgrade heads`.

```bash
MIGRATION_DATABASE_URL=postgresql://app_admin@.../dotmac_sub \
  python scripts/bootstrap_commercial_module_prereqs.py --verify-only

MIGRATION_DATABASE_URL=postgresql://app_admin@.../dotmac_sub \
  python scripts/bootstrap_outbox_dispatcher_roles.py --verify-only
```

Do not permanently grant database-level `CREATE` to `app_admin`; the bootstrap
creates/adopts the schemas and Alembic skips already-present declared module
schema creates.

Historical migration `557_outbox_relay_prereq` retains its immutable
`dotmac_app` membership prerequisite. Fresh historical replay uses the private
initializer in `scripts/ci/bootstrap_test_database_prereqs.py` before that
revision. The operational bootstrap exposes no historical mode. The CI
initializer requires the checked-in disposable cluster's postmaster-context
`cluster_name=dotmac-sub-disposable-tests` marker, a validated test endpoint
and a permitted test host. The marker is configured purpose evidence, not
authentication or execution approval; the caller still needs authorized test
credentials. It does not authorize retaining the link in a running estate.
Alembic requires both `session_user` and `current_user` to be `app_admin`
before accessing its version table. Normal dispatcher verification and repair
use `app_admin` directly, refuse the retired legacy link before writes, and
never recreate it. The definer must be able to own functions in `public`:

```bash
SELECT has_schema_privilege('app_admin', 'public', 'USAGE');
SELECT has_schema_privilege('app_admin', 'public', 'CREATE');
```

Repair applies:

```sql
GRANT USAGE, CREATE ON SCHEMA public TO app_admin;
```

Do not apply these manually as hidden deploy state. They belong to
`scripts/bootstrap_outbox_dispatcher_roles.py --repair`, and the deploy
preflight verifies them before backup.
Only the gated CI initializer prepares the old membership needed to replay
557. Its retirement in an existing estate is a separate reviewed cluster-role
operation. A test-named database on an unmarked shared server does not satisfy
the initializer's cluster boundary.

### Existing-estate cutover gate

Michael approved `app_admin` module-schema/migration ownership and `app_user`
runtime on 2026-10-01. Source alignment does not provision credentials, transfer
objects, grant legacy table access, or prove the running process identity.
The observed Seabone staging runtime still authenticates as `postgres` and
bypasses RLS. A credential-only swap is insufficient: most legacy public
tables lack `app_user` privileges.

Michael also selected permanent forward-only authority, with no compatibility
runtime or retired-writer fallback. The proposed per-object operation rules
and outstanding classifications are in
[`DATABASE_RUNTIME_ACCESS_CONTRACT.md`](../designs/DATABASE_RUNTIME_ACCESS_CONTRACT.md).
Review and retire both legacy cluster-role links; normal deployment must not
restore them. A missing grant after the cutover is repaired in the chosen
authority. A restorable backup remains a prerequisite for protecting data.

Before activating the split on an existing database, review an exact database,
source-owner and ordered ownership/grant plan, bind execution to its digest,
rehearse on disposable PostgreSQL, and verify a restorable backup plus
maintenance/quiescence. Keep the database-level owner/CREATE disposition
explicit: this repository's migration role deliberately lacks database-level
CREATE, so a generic plan that also transfers the database itself to
`app_admin` does not satisfy this contract. Never apply blanket public-table
grants: tenant and platform persistence planes retain their own contracts.

`REPORT_DATABASE_URL` with catalog visibility runs
`python -m scripts.report_database_authority`. It is read-only, has a 10-second
statement timeout and a 2,000-relation ceiling, and emits named owners and
effective privileges without rows or connection values. Effective privileges
include ownership/membership; this observation is one input to a reviewed
plan, not a direct ACL grant plan or authorization to execute it. Reobserve
the actual app and worker login after deployment and exercise positive and
negative RLS paths as that login before template adoption.

The local `scripts/testing/test_stack.sh` uses a separate database on the
existing local cluster. It requires an already-provisioned `app_user` runtime
and process-held `app_admin` migration URL for the exact `dotmac_test`
endpoint. Its create path installs only database-local extensions; it never
uses the disposable-CI role/password bootstrap against that shared cluster.
Required schema and runtime grants remain separately provisioned. A disposable
database does not make its server's roles or passwords disposable.

The digest-pinned legacy shadow stack and temporary prerequisite-repair
workflow cannot satisfy this authority split. Their migration/repair paths
now refuse explicitly; existing running hosts are not modified. Re-enabling
either needs a separately reviewed image and bootstrap contract. Do not repin
an image to work around the refusal.

## Post-migration resume

A failed production run may be resumed without another full backup only when
the failure happened after the backup and after `alembic upgrade heads`
completed. The workflow input is `resume_after_migration=true` with the prior
failed run ID and the on-host backup artifact path from that same run.

Resume is refused unless all of these are true:

- the same production authorization run is used;
- the same candidate digest is used;
- the named backup artifact exists and names the failed run ID. The official
  workflow sets `DB_BACKUP_BASENAME=dotmac_sub_run_<run-id>` so this is
  machine-checkable;
- database Alembic heads equal the candidate image heads;
- the current app image is either the previous authorized image or the
  candidate image.

When accepted, the deploy skips backup and migration only. Candidate warm-up,
service replacement, health gates, worker verification, and rollback handling
still run.

## Service-extension duplicate reconciliation

Migration 417 requires one
`(service_extension_id, subscription_id)` entry. The deployment owner runs the
candidate image's read-only check before Alembic:

```bash
python -m scripts.migration.reconcile_service_extension_duplicates --check
```

If it reports candidates, do not use direct `DELETE` or `UPDATE` SQL. Preview
the complete cohort with the candidate image, review the exact fingerprint and
dispositions, then apply through `financial.service_extensions`:

```bash
python -m scripts.migration.reconcile_service_extension_duplicates

python -m scripts.migration.reconcile_service_extension_duplicates \
  --apply \
  --fingerprint <reviewed-sha256> \
  --effective-at <iso-8601-with-timezone> \
  --idempotency-key <stable-key> \
  --actor <operator-id> \
  --reason <reviewed-reason> \
  --preserve-chained-entitlement
```

Apply collapses exact copies and preserves any approved chained interval as a
separately audited corrective extension. It does not shorten the current
customer billing anchor. Run `--check` again and require zero candidates before
retrying the guarded deployment.

## Migration/index invariant

Concurrent PostgreSQL index creation is not complete until the catalog reports
both `indisvalid` and `indisready`, and the index definition matches its
checked-in structural contract. A retry must remove an interrupted build before
recreating it; index-name existence alone is not success.

Run the read-only verification independently with:

```bash
docker compose -f docker-compose.yml run --rm --no-deps app \
  python -m scripts.migration.verify_schema_contracts

docker compose -f docker-compose.yml run --rm --no-deps app \
  python -m scripts.integrations.verify_manifest_pins
```

## Working-tree drift detection

Code deploys are immutable images. The GitHub Actions release checkout is the
source for `docker-compose.yml`; the persistent host directory supplies `.env`,
an optional host-specific Compose override, `config/`, and `nginx/`. The deploy
compares the release checkout's Git tree with the image's source-tree label
before backup or migration, so the base Compose contract and image cannot come
from different releases.

The remaining host-owned files are still operational configuration and must be
kept reviewed and clean. A host tree left on a feature branch or carrying
hand-applied edits remains configuration drift, but it can no longer replace
the authorized release's base Compose service graph during a controlled deploy.

`scripts/ops/prod_tree_drift_metrics.sh` exports that state as gauges
(`deploy_tree_on_main`, `deploy_tree_clean`, `deploy_tree_matches_origin_main`,
`deploy_tree_behind_commits`, `deploy_tree_dirty_files`,
`deploy_tree_fetch_ok`) to the host's VictoriaMetrics. Install it on the
deploy host as a root cron entry:

```
*/15 * * * * /root/dotmac_sub/scripts/ops/prod_tree_drift_metrics.sh >> /var/log/dotmac_tree_drift.log 2>&1
```

Intended alert rules (ops wiring lives on the observe host):

```promql
# Tree drifted: wrong branch, dirty, or not at origin/main for 6h
min without() (deploy_tree_on_main) == 0
min without() (deploy_tree_clean) == 0
min without() (deploy_tree_matches_origin_main) == 0
# Exporter dead or cron removed
absent_over_time(deploy_tree_clean[2h])
```

Six hours tolerates a deliberate in-progress operation; past incidents left the
tree drifted for days undetected.

## Failure behavior

- Unreadable Docker state, ambiguous container inventory, failed container
  inspection, or a missing/malformed running revision label stops in
  `scripts/deploy_production.sh` before any image pull, backup, or migration.
  Confirmed container absence also stops unless an exact typed bootstrap
  authorization is supplied.
- Migration, schema verification, or unavailable integration-pin failure
  occurs before service replacement.
- Commercial module prerequisite or dispatcher-role failure occurs before
  database backup and before Alembic. Run the explicit bootstrap repair, then
  rerun the guarded deploy.
- Candidate startup failure leaves the primary release serving traffic.
- Primary health failure restores the previous image while the candidate
  continues serving, then removes the candidate after the rollback is healthy.
- Celery worker or Beat startup/readiness failure follows the same rollback
  path. A healthy web endpoint cannot make a release acceptable while
  background processing is unavailable.
- Database migrations are forward-only and are not rolled back automatically,
  so every release migration must remain compatible with the previous image.


The current executor preflight also refuses cluster `CREATEDB`/`CREATEROLE`
and effective database `CREATE` for `app_admin`. Managed `--repair-schemas`
authenticates as `dotmac_schema_bootstrap` itself: LOGIN, NOINHERIT,
NOSUPERUSER, NOBYPASSRLS, NOCREATEDB and NOCREATEROLE, plus owner membership
and a named CREATE grant on this database. Substituting a privileged login
is refused even if it can SET ROLE. Historical revision 546 retains its
three-flag compatibility reader; the current bootstrap checks all five flags.

Disposable CI provisioning creates a missing synthetic schema-bootstrap login
with the test server's held password and owner membership. It refuses drift
on an existing login, and does not rotate that login's password. This helper
is not the provisioning path for staging or an existing developer cluster.
