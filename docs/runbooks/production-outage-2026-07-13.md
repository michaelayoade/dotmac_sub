# Production Outage: 2026-07-13

## Root Cause

The primary failure was a pagination contract defect between `dotmac_crm` and
`dotmac_sub`:

- `GET /api/v1/crm/subscribers/online?page=N` and
  `GET /api/v1/crm/locations?page=N` accepted `page` but returned the same
  complete first result set for every value of `N`.
- The CRM client interpreted every non-empty response as another page. Production
  traffic reached online page 886 and locations page 1665.
- Repeated materialization of subscriber, session, invoice, and location data
  saturated PostgreSQL CPU and the application's SQLAlchemy pools. Nginx then
  returned 504 while requests waited for database connections.

The full `pg_dump` started during recovery did not initiate this outage. It did
add database and disk load while the system was already saturated, and materially
delayed recovery. A separate 2026-07-12 incident also showed that concurrent
deploy-time dumps can cause an outage by themselves.

## Corrections

- Subscriber PR #1235 makes both materialized endpoints honor `page` and
  `per_page`, and returns `meta.total`.
- CRM PR #243 treats repeated data or a non-advancing response page as an
  upstream failure on the second request. It also fails closed on safety caps
  and paginates locations correctly.
- Deployments no longer create full logical dumps. They verify a recent usable
  backup and WAL archive before migrations.
- pgBackRest provides continuous WAL archiving, weekly full, daily differential,
  and six-hour incremental backups. Backup jobs are serialized and run at low
  CPU/I/O priority.

## Backup Architecture

PostgreSQL writes WAL continuously to an encrypted pgBackRest repository at
`/var/backups/pgbackrest`. pgBackRest keeps two full backup sets and six daily
differential sets; dependent incremental backups and required WAL are expired
with their parent sets.

The database image preserves the exact production PostgreSQL 16/PostGIS 3.4
base digest. pgBackRest 2.58 is compiled from checksum-pinned official source in
a separate build stage, enabling block-level incrementals without combining the
backup rollout with a PostgreSQL or PostGIS upgrade.

VictoriaMetrics receives backup completion, duration, repository health, archive
failure, and restore-verification metrics. Promtail ships structured operational
logs from `/var/log/dotmac_sub/pgbackrest-operations.log`.

The local repository is the fast recovery tier, not the only disaster-recovery
copy. Keep the existing off-host rclone logical backup enabled until a dedicated
pgBackRest S3 repository has been provisioned and its first off-host restore has
passed. Do not repurpose the application upload bucket for database backups.

## Production Rollout

Run from the production checkout in a maintenance window. The PostgreSQL
container recreation causes a brief reconnect window; the first full backup and
restore verification run online afterward.

```bash
cd /root/dotmac_sub
git fetch origin
# Update the operational checkout to the merged commit without discarding
# production-local work; use a clean worktree when the checkout is dirty.

sudo RUN_RESTORE_VERIFY=1 bash scripts/backup/rollout_pgbackrest.sh
```

The rollout does the following in order:

1. Verifies free disk is at least twice the current PostgreSQL data size.
2. Reads or creates `secret/backups/postgres#repo1_cipher_pass` in OpenBao.
3. Builds and validates the pgBackRest-enabled image before touching PostgreSQL.
4. Recreates only PostgreSQL, creates the stanza, and proves WAL archiving.
5. Takes the first online full backup and passes the freshness gate.
6. Sets `BACKUP_MODE=pgbackrest`, installs timers, and optionally restores the
   backup into a network-isolated temporary PostgreSQL for verification.

Never replace the OpenBao repository cipher passphrase while retained backups
exist. Losing or changing it makes those backups unreadable.

## Operations

```bash
# Repository, latest backup, and WAL archive gate
bash scripts/backup/pgbackrest_health.sh --gate

# Backup inventory
docker exec --user postgres dotmac_pg_local \
  pgbackrest --stanza=dotmac-sub info

# Manual incremental backup
bash scripts/backup/pgbackrest_backup.sh incr

# Manual isolated restore drill
bash scripts/backup/pgbackrest_restore_verify.sh --force

# Timers and recent logs
systemctl list-timers 'dotmac-pgbackrest-*'
journalctl -u 'dotmac-pgbackrest-*' --since today
```

`SKIP_BACKUP_CHECK=1` is an emergency-only deploy override. It does not create a
backup and must be recorded in the incident timeline when used.
