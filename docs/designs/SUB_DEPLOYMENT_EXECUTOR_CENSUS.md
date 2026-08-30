# Sub deployment executor census

Prepared for the controller cutover (Sub is third: Platform CP → ERP → Sub).
Preparation only — **nothing is deleted, frozen or retired here.** The
entrypoint-family ratchets and the receipt schema belong to a separate lane;
this is the census they consume.

Measured at `d021e33c9`.

## The seven families

### 1. Workflow

| Workflow | Runner | Role |
|---|---|---|
| `production-deploy.yml` | `[self-hosted, linux, x64, dotmac-sub-production]` (job at line 164) | invokes `bash scripts/deploy_production.sh` (line 273) |
| `staging-deploy.yml` | `[self-hosted, linux, x64, dotmac-sub-staging]` (line 167) | staging deployment |
| `release-promotion.yml`, `release-candidate.yml`, `release-freeze-gate.yml` | hosted | release gating, not deployment |
| `field-app-release.yml`, `mobile.yml` | hosted | mobile artifacts — a separate signing lane |

> **Discrepancy to settle.** The target map records the production runner label
> as `selfcare-dotmac-sub-production`. The workflow requires
> `dotmac-sub-production`. Either the runner carries both labels or one of the
> two is wrong; a label mismatch is a deployment that silently never picks up.

### 2. Script

`scripts/deploy_production.sh` — the direct production path named for
retirement at step 6. `scripts/deploy.sh` and `scripts/deploy_staging.sh`
alongside it. `scripts/deploy_shared_architecture.sh` also runs compose.

`scripts/deploy.sh` carries real operational knowledge worth preserving into
the controller rather than losing: an orphaned-`pg_dump` guard that refuses to
deploy while a dump is running (lines 137-144, after a dump drove load to 52 on
16 cores), and post-deploy cleanup of a dump orphaned by a dropped SSH session
(lines 852-861).

### 3. Cron

- `scripts/backup/install_dotmac_sub_backup_cron.sh` installs a crontab line
  for the rclone backup.
- `scripts/ops/prod_tree_drift_metrics.sh` documents `*/15 * * * *` in its
  header (line 13).

Both are host crontab entries — declarations outside the repository's control,
which is why a source sweep cannot see whether they are installed.

### 4. Systemd unit

`deploy/systemd/dotmac-reconcile-sweeper.service` — one unit.

### 5. SSH credential

**No SSH deployment credential exists in GitHub secrets.** The only
`secrets.*KEY*`/`*PASSWORD*` references across all workflows are Android
signing material (`ANDROID_*`, `FIELD_ANDROID_*`). Production reaches the host
through a **self-hosted runner**, not over SSH from CI.

The SSH path that does exist is the operator's own — a human on `~/.ssh`. That
is the credential family to retire at step 6, and it is not revocable from a
GitHub settings page.

### 6. Webhook

No inbound deployment webhook. Deployment is workflow- or operator-initiated.

### 7. Manual runbook

`docs/runbooks/PRODUCTION_DEPLOYMENT.md` and `STAGING_PROMOTION.md` carry
executable deployment steps. `CRM_TICKET_CAPABILITY_CUTOVER.md`,
`SERVICE_TEAM_PARTY_CUTOVER.md` and `SUB_THIN_SHADOW.md` carry operational
steps that reach the host.

A runbook is an executor. Retiring the script without retiring the runbook that
tells an operator to run it leaves the instruction standing.

## The backup defect, and it is worse than a missing-roles error

Both backup paths, confirmed in the tree:

| Script | Line | Command |
|---|---|---|
| `scripts/db_backup.sh` | 68 | `pg_dump -U … -d … --no-owner --no-privileges` |
| `scripts/backup/backup_dotmac_sub_dbs_to_rclone.sh` | 62 | `pg_dump -U … -d …` |

`pg_dump -d` never emits role definitions — those come from
`pg_dumpall --globals-only`/`--roles-only`, which neither script runs. That is
the defect that produced 114 missing-role errors on Vendor CP.

**`db_backup.sh` additionally passes `--no-owner --no-privileges`**, which
strips ownership and every `GRANT` from the dump as well. A restore from it
yields tables and rows with **no roles, no ownership and no privileges at all**.

For Sub that is not cosmetic. Tenant isolation here is **RLS plus role
grants** — FORCEd row-level security evaluated against an application role, and
platform catalog tables protected by `REVOKE` from that role rather than by
RLS. RLS policies are table-level objects and would restore; the roles they
name would not exist. A restore therefore produces a database that looks
complete and whose isolation model is absent.

Nothing links the roles back either: role creation is spread across
`scripts/bootstrap_commercial_module_prereqs.py`,
`scripts/bootstrap_outbox_dispatcher_roles.py` and migration
`557_outbox_relay_prereq.py`. No restore path references them.

**Adopt the recovery facility rather than repairing these two call sites** —
repairing them would leave the same class free to reappear in the next script,
and the facility is what makes `PostgresRecoveryBundleV1` provable.

## What is blocked, and on what

| Item | Blocked on |
|---|---|
| Pin Foundation a2 and Control a6 **independently** | `dotmac-deployment-foundation` is **not currently a dependency** of Sub — `pyproject.toml` names neither package. Pinning waits on a2 being published |
| Declare web, workers, Beat/database scheduler, PostgreSQL, Redis in the descriptor | **No descriptor exists.** There is no `deploy/product.toml`; `deploy/` holds `egress-proxy`, `links.dotmac.io`, `nginx`, `observability`, `shadow`, `systemd`. The descriptor arrives with the foundation adoption |
| `PostgresRecoveryBundleV1: PROVED` | A production-shaped **restore rehearsal**, which needs a real PostgreSQL. That is CI-owned execution, not a repository change |
| Freeze direct deployment authority | Sequencing — the freeze lands before the first controller deployment, and the ratchets come from the other lane |

## Sequence constraints to preserve

The retirement order is load-bearing in **both** directions: deleting the
scripts earlier removes rollback capability, and leaving them active later
creates two executors. The legacy path loses ordinary authority first, survives
briefly as an explicitly authorized break-glass rollback, and is deleted only
after **two** proven controller cycles. Step 6 retires
`deploy_production.sh`, the direct workflow, SSH deployment, mutable checkout
operations and the obsolete secrets, schedules and runbooks — with a receipt
naming both controller receipts and everything removed. Only step 6 increments
the scoreboard.

CRM code deletion is **not** a prerequisite for this cutover. The vocabulary
freeze (`tests/architecture/test_crm_vocabulary_freeze.py`) holds that line;
deletion happens in the owning Sales/Quotes/support slices, each proving its
replacement.
