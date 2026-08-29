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
- The commercial module prerequisite bootstrap has an elevated database
  connection available only when repair is intended. `BOOTSTRAP_DATABASE_URL`
  may be injected for the deploy process, but it is not an application
  connection string and is never logged.
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
5. Run database prerequisite bootstrap if `BOOTSTRAP_DATABASE_URL` is supplied,
   then verify commercial module schemas and outbox dispatcher roles through
   the restricted migration connection. Missing prerequisites fail here before
   backup and before Alembic.
6. Back up the database.
7. Run candidate-image pre-migration state checks against the target database.
8. Pin the immutable image and revision.
9. Apply `alembic upgrade heads`, retrying bounded PostgreSQL lock timeouts.
10. Verify registered schema contracts and reject every invalid or unready
   user-schema index.
11. Verify every enabled integration installation pin resolves to a current or
   bounded historical definition in the new image. Unavailable pins block
   replacement; historical pins are reported for explicit adoption.
12. Verify that an enabled `crm.ticket_pull` control has exactly one enabled
   `crm.ticket_observation.v1` binding and one active job bound to it. Complete
   the reviewed
   [`CRM_TICKET_CAPABILITY_CUTOVER.md`](CRM_TICKET_CAPABILITY_CUTOVER.md)
   procedure with the candidate image before deployment when this gate fails.
13. Start and health-check the new application image on `127.0.0.1:18002`.
14. Recreate the primary application and workers. Nginx uses the healthy
   candidate while the primary port is unavailable.
15. Verify the primary image has no source-code bind mount and wait for its
   health endpoint.
16. Require every declared Celery worker to remain restart-free and answer a
   node-specific ping, and require Celery Beat to remain running without
   restarts, across a bounded stabilization window.
17. Gracefully drain the candidate and retain the configured rollback images.

The candidate runs the same image, environment, and database schema as the
primary. It is bound to localhost and exists only for the handoff window.

## Commercial module database prerequisites

Commercial modules are composed in shadow mode under owned `mod_*` schemas:
`mod_payments`, `mod_billing`, `mod_coll`, `mod_serviceorders`, and
`mod_subscriptions`. Their schemas and cluster roles are privileged deployment
prerequisites. Alembic runs as the restricted migration role and only verifies
that the prerequisites exist.

Repair is explicit and idempotent:

```bash
BOOTSTRAP_DATABASE_URL=postgresql://postgres@.../dotmac_sub \
  python scripts/bootstrap_commercial_module_prereqs.py --repair

BOOTSTRAP_DATABASE_URL=postgresql://postgres@.../dotmac_sub \
  python scripts/bootstrap_outbox_dispatcher_roles.py --repair
```

Verification uses the restricted migration connection:

```bash
MIGRATION_DATABASE_URL=postgresql://dotmac_app@.../dotmac_sub \
  python scripts/bootstrap_commercial_module_prereqs.py --verify-only

MIGRATION_DATABASE_URL=postgresql://dotmac_app@.../dotmac_sub \
  python scripts/bootstrap_outbox_dispatcher_roles.py --verify-only
```

The deploy owner runs the same verification before backup and before
`alembic upgrade heads`. It runs repair first only when `BOOTSTRAP_DATABASE_URL`
is present in the deploy environment. Do not permanently grant database-level
`CREATE` to `dotmac_app`; the bootstrap creates/adopts the schemas and Alembic
skips already-present declared module schema creates.

The outbox dispatcher bootstrap also owns the function-ownership prerequisites
for migration `557_outbox_relay_prereq`. The restricted migration role must be
able to become the definer, and the definer must be able to own functions in
`public`:

```bash
SELECT pg_has_role('dotmac_app', 'app_admin', 'MEMBER');
SELECT has_schema_privilege('app_admin', 'public', 'USAGE');
SELECT has_schema_privilege('app_admin', 'public', 'CREATE');
```

Repair applies:

```sql
GRANT app_admin TO dotmac_app;
GRANT USAGE, CREATE ON SCHEMA public TO app_admin;
```

Do not apply these manually as hidden deploy state. They belong to
`scripts/bootstrap_outbox_dispatcher_roles.py --repair`, and the deploy
preflight verifies them before backup.

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

docker compose -f docker-compose.yml run --rm --no-deps app \
  python -m scripts.integrations.verify_crm_ticket_readiness
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
- Migration, schema verification, unavailable integration-pin, or CRM ticket
  capability-readiness failure occurs before service replacement.
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
