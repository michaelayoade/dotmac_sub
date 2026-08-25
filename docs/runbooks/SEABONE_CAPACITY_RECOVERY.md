# Seabone capacity recovery and workload placement

Status: operator runbook

Owner: Dotmac staging platform operations

Seabone is a staging host, not a general-purpose test or migration host. This
runbook covers resource-starvation recovery, reviewed cleanup, and the target
placement of workloads currently competing on it.

## Safety boundary

Before recovering an unreachable or resource-starved host:

1. Cancel every queued Seabone deployment.
2. Set each repository's automatic staging-deploy switch to false.
3. Confirm central monitoring reports the node down or stale.
4. Use an out-of-band controller only after Michael explicitly names that
   separate target.

Never use `swapoff -a` while the host is pressured. Pulling all swapped pages
into RAM at once can freeze or OOM the host. Never use `docker volume prune`,
`docker system prune --volumes`, or Compose `--remove-orphans`. The current
Sub staging database, Redis, and OpenBao containers are load-bearing orphans
that the checked-in Compose files cannot recreate.

Do not reset, clean, stash, switch, or test in
`/home/dotmac/projects/dotmac_sub`; its host-local Compose override and `.env`
are part of the staging deployment contract.

## Read-only inventory

After SSH returns, collect evidence before changing state:

```bash
uptime
free -h
swapon --show
vmstat 1 5
df -hT
df -ih
docker stats --no-stream
docker system df -v
docker ps -a --format '{{.ID}} {{.Names}} {{.Status}} {{.Image}}'
journalctl --disk-usage
pgrep -af 'pg_dump|pg_restore|db_sync_to_staging|dotmac_data|deploy.sh'
```

Do not print `.env`, container environments, database URLs, or credential
values. Record counts, sizes, health states, and exact container/image names.
Treat resident memory and swap ownership as separate measurements: a small
resident process may still hold a large amount of stale swap.

## Stabilization order

1. Keep all nonessential stacks stopped. The previously approved stopped set is
   `schoolnet`, `dotmac_starter`, `dotmac_mkt`, `dotmac_ecm`, `dotmac_data`, and
   `dotmac-platform`.
2. If swap remains high, restart the largest swap-holding containers one at a
   time. Verify container health and `MemAvailable` before proceeding to the
   next container. Never restart the whole host workload in parallel.
3. Do not deploy or start a dump while any staging database is unhealthy or
   while blocked processes/I/O pressure exceed the admission thresholds.
4. Keep Celery Beat absent from Sub staging.
5. Re-run `scripts/staging_host_admission.py`. A zero exit is necessary but not
   sufficient for re-enabling deployment; application/database health must
   also be verified.

## Reviewed cleanup

Cleanup always begins with an exact inventory. Remove by exact ID or path; do
not use broad globs or volume pruning.

- Remove abandoned throwaway test containers, migration runners, and
  warm-candidate containers only after confirming no owning process exists.
- Remove obsolete Sub application images with
  `DRY_RUN=1 scripts/docker_image_retention.sh` first. The script preserves all
  in-use images and five unused rollback images by default.
- Inventory `/home/dotmac/backups/dotmac_sub` by filename, timestamp, size, and
  checksum. Existing staging dumps remain retained until Michael approves a
  policy and the selected copies are verified at the approved backup
  destination. A sensible target is the newest two local staging dumps after
  off-host verification.
- Remove abandoned agent worktrees only from the agent-work root and only after
  `git worktree list --porcelain` proves them prunable. Never prune the live
  project checkout.
- Audit the remaining `app_monitor` Loki, Prometheus, and Grafana containers.
  Retire them only after proving central dotmac-observe is the sole metrics,
  logs, dashboard, and alert owner and no application endpoint depends on the
  local stack.

The 8 GiB Nominatim setting is a limit, not an allocation. Measure actual
resident and swap usage before considering it a cleanup target.

## Workload placement

The approved immediate rule is:

- Ad-hoc tests, benchmark runs, dependency builds, and scratch containers run
  only on the explicitly named dotmac-observe throwaway-test host, one bounded
  container at a time. They contain no production restore or ETL data and are
  removed after the run.
- Production-data restores, Splynx restore usage, `dotmac_data`, and ETL jobs
  require a separately approved trusted migration host with dedicated storage.
  They do not move to dotmac-observe.
- Until a dedicated Sub staging VM is provisioned, CRM/ERP/Sub deployments,
  nightly syncs, restores, and migrations share
  `/var/lock/dotmac_staging_heavy.lock` and the same resource admission policy.

The target topology is to move the complete Sub staging stack—application,
database, broker, workers, network dependencies, and its repository-scoped
runner—to one dedicated private staging VM. Do not split its transaction and
deployment boundary across hosts merely to distribute memory. Moving the stack
requires a reviewed host contract, a new private address, updated GitHub
environment configuration, immutable-image acceptance, and an update to this
runbook before the old Seabone stack is retired.

## Re-enable and verify

Re-enable automatic staging deployment only when:

- central monitoring has fresh node metrics and no swap/I/O alert;
- the admission command returns allowed;
- Sub/CRM/ERP databases are healthy;
- there is no active dump, restore, sync, deploy, or data pipeline;
- all deliberately stopped stacks remain stopped; and
- the exact immutable candidate is still the current green `origin/dev` tip.

Run staging acceptance normally. A recovery or cleanup does not waive the
dev-to-staging-to-main promotion contract.
