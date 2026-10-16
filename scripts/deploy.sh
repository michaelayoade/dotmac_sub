#!/usr/bin/env bash
# Deploy dotmac_sub from a registry-built (GHCR) image — no host build.
#
# This is the RECOMMENDED production path: it runs the exact image CI built and
# tested, decoupled from the box's git working tree (which has repeatedly drifted
# — feature branches, dirty trees, hand-applied migrations). `make prod-deploy`
# (host build) is kept only as an air-gapped/registry-down fallback.
#
# Usage:
#   deploy.sh sha-abc1234        deploy an existing immutable image tag
#   deploy.sh sha256:<64-hex>    deploy the repository image by exact OCI digest
#   deploy.sh --status           show pinned vs running image
#   Production backups may be omitted only through deploy_production.sh with
#   a typed, verified no-migration hotfix decision.
#   HEALTH_CURL_TIMEOUT=N ...    cap each health-check curl attempt at N seconds
#                                (default 5) so a hung health endpoint can't stall
#                                a retry indefinitely
#   READY_URL=URL                primary readiness document (default derived
#                                from HEALTH_URL: <base>/api/v1/health/ready);
#                                the candidate uses CANDIDATE_READY_URL
#   BACKGROUND_STABILITY_SECONDS=N
#                              require workers and Beat to remain restart-free
#                              for N seconds before accepting the release
#   REQUIRE_PROXY_HANDOFF=0    skip the Nginx warm-candidate contract, for hosts
#                              with no proxy in front of the app (staging).
#                              Defaults to 1 — production must keep it.
#   IGNORE_COMPOSE_OVERRIDE=1  ignore docker-compose.override.yml even if present
#
# Compose files: docker-compose.yml, plus docker-compose.override.yml when it
# exists — matching what a bare `docker compose` on the host resolves. Services
# are then intersected with `compose config --services`, so a profile-gated
# service (e.g. staging's celery-beat) is never force-started by being named.
#
# Host-specific values (HEALTH_URL, DB_CONTAINER) fall back to .env when not
# set in the environment.
#
# RUN IT DETACHED over SSH -- `nohup ./scripts/deploy.sh sha-... &` or inside
# tmux. A dropped SSH session sends SIGHUP and kills the deploy mid-flight; the
# script now cleans up after itself, but a deploy that dies during `alembic
# upgrade heads` still leaves the schema half-applied.
#
# Procedure:
#   pull image from GHCR -> verify OCI revision + exact GitHub CI evidence ->
#   candidate secret readiness -> DB prerequisites + backup -> pin APP_IMAGE + GIT_SHA in .env ->
#   migrate + verify -> warm candidate -> recreate app+workers ->
#   web + background-runtime health gates.
#
# A failed health gate may restore the previous image only if the candidate
# proves the database has no payment-email installation floor. Migrations are
# never reverted automatically.
set -euo pipefail

# Deploy dir == repo root (sub deploys in place). Override with DEPLOY_DIR.
DEPLOY_DIR="${DEPLOY_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REPO_DIR="${REPO_DIR:-${DEPLOY_DIR}}"
cd "${DEPLOY_DIR}"

# Read a key out of .env without sourcing it (sourcing a deploy .env pulls in
# every secret and DATABASE_URL as shell state). Defined up here because
# DB_CONTAINER is needed by the orphaned-pg_dump guard below, long before the
# rest of the config block.
env_value() {
  local key="$1"
  local value
  value="$(grep -E "^${key}=" .env 2>/dev/null | tail -n 1 | cut -d= -f2- || true)"
  printf '%s\n' "${value}"
}

IMAGE_REPO="ghcr.io/michaelayoade/dotmac_sub"
APP_CONTAINER="dotmac_sub_app"
GITHUB_RELEASE_REPOSITORY="michaelayoade/dotmac_sub"
PYTHON_BIN="${PYTHON_BIN:-python3}"

run_repo_module() {
  REPO_DIR="${REPO_DIR}" PYTHON_BIN="${PYTHON_BIN}" \
    bash "${REPO_DIR}/scripts/run_repo_module.sh" "$@"
}
# Host-specific, so resolve shell env > .env > default. The default assumes the
# app publishes on loopback; staging binds its port to a host-internal address
# instead, and a wrong default here fails the gate rather than passing it.
HEALTH_URL="${HEALTH_URL:-$(env_value HEALTH_URL)}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8001/health}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-180}"
# Cold starts can include image extraction, Python imports, and application
# startup work that is materially slower than the steady-state health gate.
# Keep the candidate gate independent so a slow warm-up is not mistaken for a
# failed release, while retaining the shorter timeout for the live primary.
CANDIDATE_HEALTH_TIMEOUT_SECONDS="${CANDIDATE_HEALTH_TIMEOUT_SECONDS:-600}"
# Per-attempt cap on the health-check curl itself, distinct from the overall
# HEALTH_TIMEOUT_SECONDS retry budget above — without it a hung health
# endpoint stalls a single curl call indefinitely instead of failing fast
# into the next retry.
HEALTH_CURL_TIMEOUT="${HEALTH_CURL_TIMEOUT:-5}"
IMAGE_RETAIN_COUNT="${IMAGE_RETAIN_COUNT:-5}"
MIGRATION_MAX_ATTEMPTS="${MIGRATION_MAX_ATTEMPTS:-4}"
MIGRATION_RETRY_SECONDS="${MIGRATION_RETRY_SECONDS:-10}"
CANDIDATE_CONTAINER="${CANDIDATE_CONTAINER:-dotmac_sub_app_candidate}"
CANDIDATE_PORT="${CANDIDATE_PORT:-18002}"
CANDIDATE_HEALTH_URL="${CANDIDATE_HEALTH_URL:-http://127.0.0.1:${CANDIDATE_PORT}/health}"
# Readiness, not just liveness. `/health` proves one worker's event loop is
# serving; `/api/v1/health/ready` reports `"status":"ready"` only once that
# worker's lifespan has loaded every router (`routes_ready`, logged as
# `startup_complete`) and it can reach the database. Both web gates poll the
# readiness document inside the same timeout budget and fail closed when it
# never reports ready. The previous-image restore gate keeps liveness only,
# because an older image may predate the readiness contract.
READY_URL="${READY_URL:-$(env_value READY_URL)}"
READY_URL="${READY_URL:-${HEALTH_URL%/health}/api/v1/health/ready}"
CANDIDATE_READY_URL="${CANDIDATE_READY_URL:-http://127.0.0.1:${CANDIDATE_PORT}/api/v1/health/ready}"
CANDIDATE_DRAIN_SECONDS="${CANDIDATE_DRAIN_SECONDS:-2}"
BACKGROUND_RUNTIME_TIMEOUT_SECONDS="${BACKGROUND_RUNTIME_TIMEOUT_SECONDS:-90}"
BACKGROUND_STABILITY_SECONDS="${BACKGROUND_STABILITY_SECONDS:-15}"
CELERY_INSPECT_TIMEOUT_SECONDS="${CELERY_INSPECT_TIMEOUT_SECONDS:-5}"
PRODUCTION_DEPLOY_DRAIN_BUDGET_SECONDS=600
PRODUCTION_DEPLOY_CLOCK_SKEW_ALLOWANCE_SECONDS=30
# Reserved after drain: 20m migration, 10m warm candidate health, 10m
# primary health, 10m recovery, and 27.5m for bounded stop/schema/start work.
PRODUCTION_DEPLOY_CUTOVER_RESERVE_SECONDS=4650
PRODUCTION_DEPLOY_MIGRATION_BUDGET_SECONDS=1200
# Every service that runs the app image and must be recreated on a new build.
#
# Note what is NOT here, because it is load-bearing and easy to miss:
# postgres-local, redis-local, nominatim, freeradius, genieacs and the metrics
# services are absent. A deploy never recreates them. So a change to one of
# those service definitions -- a published port, a volume, a command flag --
# does NOT take effect by deploying. The repository looking fixed is not the
# same as the host being fixed.
#
# For PUBLISHED PORTS there is now a managed path for exactly this, and it is
# the one to use -- not a hand `up -d` on the box:
#
#   scripts/reconcile_published_ports.sh --service <name> --environment <env>
#
# It applies deploy/published_ports.toml, proves the environment value is what
# compose actually resolves BEFORE recreating, and re-reads the host's actual
# listeners afterwards. See docs/runbooks/PUBLISHED_PORT_RECONCILE.md and
# ADR-0014. Everything else about these services still needs a deliberate,
# separately scheduled recreate.
#
# Their release-shipped CONFIG FILES are different: a deploy restarts (never
# recreates) freeradius, vmagent and promtail when a checkout file they mount
# changed after they started. See apply_service_config_changes below and
# docs/runbooks/SERVICE_CONFIG_MOUNTS.md.
APP_SERVICES=(app celery-worker celery-worker-bandwidth celery-worker-ingestion \
  celery-worker-monitoring celery-worker-notifications-immediate \
  celery-worker-notifications celery-worker-billing \
  celery-worker-tr069 celery-beat \
  bandwidth-poller syslog-listener)
CELERY_WORKER_SERVICES=(celery-worker celery-worker-bandwidth \
  celery-worker-ingestion celery-worker-monitoring \
  celery-worker-notifications-immediate celery-worker-notifications \
  celery-worker-billing celery-worker-tr069)
CELERY_BEAT_SERVICE="celery-beat"
PRODUCTION_PRODUCER_SERVICES=(app bandwidth-poller syslog-listener team-inbox-smtp)

DB_CONTAINER="${DB_CONTAINER:-$(env_value DB_CONTAINER)}"
DB_CONTAINER="${DB_CONTAINER:-dotmac_pg_local}"

# Compose prefers a DATABASE_URL exported in the shell over .env, so alembic
# (and the app) could target a different database than the one every .env-based
# check and backup in this script reads. Refuse before anything is changed.
# Neither value is printed.
if [[ -n "${DATABASE_URL+x}" && "${DATABASE_URL}" != "$(env_value DATABASE_URL)" ]]; then
  echo "DEPLOY REFUSED: DATABASE_URL is exported in the environment and differs from .env; compose would use it instead of .env. Unset it and retry." >&2
  exit 1
fi

# --- One deploy at a time -------------------------------------------------
#
# Nothing used to stop two deploys running at once. On 2026-07-12 two did: each
# started a full pg_dump of the production database, load hit 52 on 16 cores,
# the app was starved out and the site served 502s for ~10 minutes. Had both
# reached `alembic upgrade heads` they would have raced the migration chain
# against the same database.
LOCK_FILE="${DEPLOY_LOCK_FILE:-/var/lock/dotmac_sub_deploy.lock}"
command -v flock >/dev/null || {
  # Fail closed, but say WHY. Without this, `! flock` succeeds on a missing
  # binary and the script claims another deploy holds the lock -- sending you
  # to hunt a process that does not exist.
  echo "REFUSING TO DEPLOY: flock(1) not found; cannot guarantee only one deploy runs." >&2
  echo "Install util-linux, or set DEPLOY_LOCK_FILE= to opt out (NOT recommended)." >&2
  exit 1
}
if ! { exec 9>"${LOCK_FILE}"; } 2>/dev/null; then
  echo "Cannot open deploy lock ${LOCK_FILE}" >&2
  exit 1
fi
if ! flock -n 9; then
  echo "REFUSING TO DEPLOY: another deploy already holds ${LOCK_FILE}." >&2
  pgrep -af "scripts/deploy.sh" | grep -v "^$$ " | sed "s/^/  running: /" >&2 || true
  exit 1
fi

# A pg_dump left behind by a deploy that died (e.g. its SSH session dropped)
# outlives its parent and keeps hammering the DB. The lock will not catch that
# -- the dead process released it. Refuse to pile a second dump on top.
if pgrep -f "pg_dump .*-d ${DB_NAME:-dotmac_sub}" >/dev/null 2>&1; then
  echo "REFUSING TO DEPLOY: a pg_dump is already running against ${DB_NAME:-dotmac_sub}." >&2
  pgrep -af "pg_dump" | sed "s/^/  /" >&2
  echo "It is probably orphaned from a deploy that died. Kill it, then retry:" >&2
  echo "  pkill -f pg_dump; docker exec ${DB_CONTAINER} pkill -f pg_dump" >&2
  exit 1
fi

log() { printf '\n==> %s\n' "$*"; }

wait_for_health() {
  local url="$1"
  local label="$2"
  local watched_container="${3:-}"
  local timeout_seconds="${4:-${HEALTH_TIMEOUT_SECONDS}}"
  local ready_url="${5:-}"
  if [[ ! "${timeout_seconds}" =~ ^[0-9]+$ ]]; then
    echo "${label} health gate misconfigured: timeout must be a non-negative integer" >&2
    return 1
  fi
  local deadline=$((SECONDS + timeout_seconds))
  local state
  local ready_body=""
  local last_readiness="not polled (liveness never answered)"
  while true; do
    if curl -fsS --connect-timeout "${HEALTH_CURL_TIMEOUT}" \
      --max-time "${HEALTH_CURL_TIMEOUT}" -o /dev/null "${url}" 2>/dev/null; then
      if [[ -z "${ready_url}" ]]; then
        return 0
      fi
      # A non-2xx answer (still starting, throttled, DB unreachable) is "not
      # ready yet", never success.
      ready_body="$(curl -fsS --connect-timeout "${HEALTH_CURL_TIMEOUT}" \
        --max-time "${HEALTH_CURL_TIMEOUT}" "${ready_url}" 2>/dev/null || true)"
      if grep -Eq '"status"[[:space:]]*:[[:space:]]*"ready"' <<<"${ready_body}"; then
        return 0
      fi
      last_readiness="$(head -c 300 <<<"${ready_body:-no 2xx response}")"
    fi
    if [[ -n "${watched_container}" ]]; then
      state="$(docker inspect "${watched_container}" \
        --format '{{.State.Status}}' 2>/dev/null || true)"
      if [[ "${state}" != "running" ]]; then
        echo "${label} stopped before becoming healthy: ${state:-unavailable}" >&2
        return 1
      fi
    fi
    if ((SECONDS >= deadline)); then
      echo "${label} health gate failed: ${url} (timeout ${timeout_seconds}s)" >&2
      if [[ -n "${ready_url}" ]]; then
        echo "${label} readiness never reported ready at ${ready_url}; last answer: ${last_readiness}" >&2
      fi
      return 1
    fi
    sleep 5
  done
}

report_candidate_failure() {
  echo "Warm candidate container state:" >&2
  if ! docker inspect "${CANDIDATE_CONTAINER}" \
    --format 'status={{.State.Status}} exit_code={{.State.ExitCode}} oom_killed={{.State.OOMKilled}} restarting={{.State.Restarting}} error={{json .State.Error}}' \
    >&2; then
    echo "  unavailable (container may have exited before inspection)" >&2
  fi
  echo "Warm candidate logs (last 200 lines):" >&2
  if ! docker logs --tail 200 "${CANDIDATE_CONTAINER}" >&2; then
    echo "  unavailable (container produced no readable logs)" >&2
  fi
}

require_candidate_health() {
  if wait_for_health \
    "${CANDIDATE_HEALTH_URL}" "Warm candidate" "${CANDIDATE_CONTAINER}" \
    "${CANDIDATE_HEALTH_TIMEOUT_SECONDS}" "${CANDIDATE_READY_URL}"; then
    return 0
  fi
  # Capture bounded diagnostics before the ERR trap evaluates the rollback
  # floor. Application logging rules
  # prohibit secret material, so this emits runtime logs rather than env/config.
  report_candidate_failure
  return 1
}

service_container_id() {
  local service="$1"
  "${COMPOSE[@]}" ps -q "${service}" 2>/dev/null | tail -n 1
}

assert_background_service_running() {
  local service="$1"
  local container
  local state
  local restart_count

  container="$(service_container_id "${service}")"
  if [[ -z "${container}" ]]; then
    echo "BACKGROUND RUNTIME FAILURE: ${service} has no Compose container." >&2
    return 1
  fi
  state="$(docker inspect "${container}" \
    --format '{{.State.Status}}' 2>/dev/null || true)"
  restart_count="$(docker inspect "${container}" \
    --format '{{.RestartCount}}' 2>/dev/null || true)"
  if [[ "${state}" != "running" ]]; then
    echo "BACKGROUND RUNTIME FAILURE: ${service} is ${state:-unavailable}." >&2
    return 1
  fi
  if [[ ! "${restart_count}" =~ ^[0-9]+$ ]] || ((restart_count != 0)); then
    echo "BACKGROUND RUNTIME FAILURE: ${service} restart count is ${restart_count:-unknown}; expected 0." >&2
    return 1
  fi
}

background_runtime_ready() {
  local service
  local include_beat="${1:-1}"
  local container
  local hostname
  local probe_container=""
  local -a expected_nodes=()
  local -a queue_requirements=()
  local required_queue=""

  # Beat is profile-gated off on staging (it must never originate production
  # work there), so assert it only where it is actually declared.
  if [[ "${include_beat}" == 1 ]] && service_is_declared "${CELERY_BEAT_SERVICE}"; then
    assert_background_service_running "${CELERY_BEAT_SERVICE}" || return 1
  fi
  for service in "${CELERY_WORKER_SERVICES[@]}"; do
    assert_background_service_running "${service}" || return 1
    container="$(service_container_id "${service}")"
    hostname="$(docker inspect "${container}" \
      --format '{{.Config.Hostname}}' 2>/dev/null || true)"
    if [[ -z "${hostname}" ]]; then
      echo "BACKGROUND RUNTIME FAILURE: cannot resolve ${service} Celery node name." >&2
      return 1
    fi
    if [[ -z "${probe_container}" ]]; then
      probe_container="${container}"
    fi
    expected_nodes+=("celery@${hostname}")
    case "${service}" in
      celery-worker-notifications-immediate)
        required_queue="notifications_immediate"
        ;;
      celery-worker-notifications)
        required_queue="notifications"
        ;;
      *)
        required_queue=""
        ;;
    esac
    if [[ -n "${required_queue}" ]]; then
      queue_requirements+=(--require-queue "celery@${hostname}=${required_queue}")
    fi
  done

  if ! docker exec "${probe_container}" \
    python -m scripts.verify_celery_workers \
    --timeout "${CELERY_INSPECT_TIMEOUT_SECONDS}" \
    "${queue_requirements[@]}" \
    "${expected_nodes[@]}" >/dev/null 2>&1; then
    echo "BACKGROUND RUNTIME FAILURE: a Celery worker did not answer ping or consume its required queue." >&2
    return 1
  fi
}

wait_for_background_workers() {
  local deadline=$((SECONDS + BACKGROUND_RUNTIME_TIMEOUT_SECONDS))
  until background_runtime_ready 0; do
    if ((SECONDS >= deadline)); then
      echo "BACKGROUND RUNTIME FAILURE: workers did not become ready before the bounded deadline." >&2
      return 1
    fi
    sleep 2
  done
}

wait_for_background_runtime() {
  local deadline=$((SECONDS + BACKGROUND_RUNTIME_TIMEOUT_SECONDS))

  until background_runtime_ready; do
    if ((SECONDS >= deadline)); then
      echo "Background runtime health gate failed after ${BACKGROUND_RUNTIME_TIMEOUT_SECONDS}s." >&2
      return 1
    fi
    sleep 5
  done

  if ((BACKGROUND_STABILITY_SECONDS > 0)); then
    log "Celery workers and Beat are ready; verifying ${BACKGROUND_STABILITY_SECONDS}s restart-free stability"
    sleep "${BACKGROUND_STABILITY_SECONDS}"
  fi
  if ! background_runtime_ready; then
    echo "Background runtime health gate failed during the restart-free stability check." >&2
    return 1
  fi
}

# The warm-candidate handoff keeps the site served while Compose replaces the
# primary, so it is required wherever the app is public-facing. Staging has no
# nginx in front of it and nothing to keep warm; without an opt-out the gate
# aborts the deploy on its very first step. Default 1 preserves the production
# contract exactly -- a host must opt OUT deliberately.
assert_proxy_handoff_contract() {
  local config
  if [[ "${REQUIRE_PROXY_HANDOFF:-1}" != "1" ]]; then
    log "Skipping Nginx warm-handoff contract (REQUIRE_PROXY_HANDOFF=${REQUIRE_PROXY_HANDOFF})"
    return 0
  fi
  if ! command -v nginx >/dev/null; then
    echo "DEPLOY AVAILABILITY FAILURE: nginx is not installed or not on PATH." >&2
    return 1
  fi
  if ! config="$(nginx -T 2>&1)"; then
    echo "DEPLOY AVAILABILITY FAILURE: nginx configuration cannot be read." >&2
    echo "Run 'nginx -T' on the host for the configuration error." >&2
    return 1
  fi
  if ! grep -Eq \
    "^[[:space:]]*server[[:space:]]+127\\.0\\.0\\.1:${CANDIDATE_PORT}[[:space:]]+backup" \
    <<<"${config}"; then
    echo "DEPLOY AVAILABILITY FAILURE: nginx has no warm candidate upstream on" >&2
    echo "127.0.0.1:${CANDIDATE_PORT}. Install nginx/selfcare.dotmac.io.conf and reload nginx." >&2
    return 1
  fi
}

assert_candidate_port_available() {
  local port_owner

  docker rm -f "${CANDIDATE_CONTAINER}" >/dev/null 2>&1 || true
  port_owner="$(
    docker ps --format '{{.Names}} {{.Ports}}' \
      | grep -E "(^|[ ,])127\\.0\\.0\\.1:${CANDIDATE_PORT}->|(^|[ ,])0\\.0\\.0\\.0:${CANDIDATE_PORT}->|\\[::\\]:${CANDIDATE_PORT}->" \
      || true
  )"
  if [[ -n "${port_owner}" ]]; then
    echo "DEPLOY AVAILABILITY FAILURE: candidate port 127.0.0.1:${CANDIDATE_PORT} is already allocated." >&2
    printf '%s\n' "${port_owner}" | sed 's/^/  owner: /' >&2
    echo "Set CANDIDATE_PORT to a free warm-candidate port before retrying." >&2
    return 1
  fi
  if command -v ss >/dev/null \
    && ss -H -ltn | awk '{print $4}' | grep -Eq "(^|:|\\])${CANDIDATE_PORT}$"; then
    echo "DEPLOY AVAILABILITY FAILURE: candidate port 127.0.0.1:${CANDIDATE_PORT} is already in use by a host process." >&2
    echo "Set CANDIDATE_PORT to a free warm-candidate port before retrying." >&2
    return 1
  fi
}

CANDIDATE_STARTED=0
PRIMARY_REPLACED=0

cleanup_candidate() {
  if [[ "${CANDIDATE_STARTED}" == "1" ]]; then
    docker rm -f "${CANDIDATE_CONTAINER}" >/dev/null 2>&1 || true
    CANDIDATE_STARTED=0
  fi
}

stop_candidate_gracefully() {
  if [[ "${CANDIDATE_STARTED}" != "1" ]]; then
    return
  fi
  sleep "${CANDIDATE_DRAIN_SECONDS}"
  docker stop --time 30 "${CANDIDATE_CONTAINER}" >/dev/null 2>&1 || true
  docker rm -f "${CANDIDATE_CONTAINER}" >/dev/null 2>&1 || true
  CANDIDATE_STARTED=0
}

MIGRATION_RUN_PREFIX=""
MIGRATION_INVOCATION_STARTED=0
run_migrations() {
  if [[ "${CUTOVER_PHASE:-idle}" != idle ]]; then
    local -a args=(--deploy-directory "${DEPLOY_DIR}" \
      --binding-json "${CUTOVER_BINDING_JSON}" \
      --run-id "${DEPLOY_RUN_ID:-0}" \
      --authorization-run-id "${AUTHORIZATION_RUN_ID:-0}" \
      --target-revision "${FULL_SHA}" \
      --candidate-digest "${IMAGE##*@}" \
      --budget-seconds "${PRODUCTION_DEPLOY_MIGRATION_BUDGET_SECONDS}")
    if [[ "${DOTMAC_MAINTENANCE_CONTEXT_FD:-}" == 7 ]]; then
      args+=(--maintenance-context-fd 7)
    fi
    local head
    while IFS= read -r head; do
      [[ -n "${head}" ]] && args+=(--heads-before "${head}")
    done <<<"${MIGRATION_HEADS_BEFORE}"
    while IFS= read -r head; do
      [[ -n "${head}" ]] && args+=(--candidate-head "${head}")
    done < <(candidate_heads)
    MIGRATION_INVOCATION_STARTED=1
    run_repo_module scripts.production_deploy_migration run "${args[@]}"
    return $?
  fi
  local attempt=1
  MIGRATION_RUN_PREFIX="dotmac_sub_migrate_${DEPLOY_RUN_ID:-$$}_"
  local output
  local rc
  while ((attempt <= MIGRATION_MAX_ATTEMPTS)); do
    if output="$("${COMPOSE[@]}" run --rm --no-deps \
      --name "${MIGRATION_RUN_PREFIX}${attempt}" app alembic upgrade heads 2>&1)"; then
      printf '%s\n' "${output}"
      return 0
    else
      rc=$?
    fi
    printf '%s\n' "${output}" >&2
    if ! grep -qiE "lock timeout|canceling statement due to lock" <<<"${output}"; then
      return "${rc}"
    fi
    if ((attempt == MIGRATION_MAX_ATTEMPTS)); then
      echo "Migration remained lock-blocked after ${MIGRATION_MAX_ATTEMPTS} attempts." >&2
      return "${rc}"
    fi
    log "Migration lock timeout (attempt ${attempt}/${MIGRATION_MAX_ATTEMPTS}); retrying in ${MIGRATION_RETRY_SECONDS}s"
    sleep "${MIGRATION_RETRY_SECONDS}"
    attempt=$((attempt + 1))
  done
}

# --- Module database prerequisites ----------------------------------------
#
# Schemas and cluster roles are privileged DEPLOYMENT prerequisites, not
# Alembic effects: the restricted migration role deliberately never holds
# database-level CREATE (ADR-0011). Until 2026-08-31 the repair leg below
# short-circuited whenever no elevated credential was configured, and the
# deploy carried on to a verification it could not possibly satisfy:
# "nothing to do" and "nothing can be done" both returned 0.
#
# What that cost: 20e1fb9a6 (#2819) composed the dotmac-inbox lineage, which
# added mod_inbox to the derived contract. No environment had it. Two staging
# deploys then failed at the verification gate. Production was never attempted
# - the gate stopped the pipeline first, which is the gate working - and it
# remains unprovisioned to this day.
#
# The leg now reports one of three outcomes and never conflates them:
#   already_satisfied - the contract holds; nothing was written
#   repaired          - the managed credential brought it to contract
#   blocked           - repair is required and cannot proceed; the deploy stops
PREREQUISITE_OUTCOME="unknown"

# Where the deployment adapter finds the dedicated schema-creation credential.
# Root-owned, 0400, never readable by the app, the runner or dotmac_app. The
# password is only ever read by libpq from this file - never the URL, argv,
# environment or log.
SCHEMA_BOOTSTRAP_PGPASS="${SCHEMA_BOOTSTRAP_PGPASS:-$(env_value SCHEMA_BOOTSTRAP_PGPASS)}"
SCHEMA_BOOTSTRAP_PGPASS="${SCHEMA_BOOTSTRAP_PGPASS:-/etc/dotmac/sub/schema-bootstrap.pgpass}"
SCHEMA_BOOTSTRAP_PGPASS_MOUNT="${SCHEMA_BOOTSTRAP_PGPASS_MOUNT:-/run/secrets/schema-bootstrap.pgpass}"
SCHEMA_BOOTSTRAP_URL="${SCHEMA_BOOTSTRAP_URL:-$(env_value SCHEMA_BOOTSTRAP_URL)}"
# The fixed user that owns the credential and runs the deployment adapter.
# Overridable because a host may run the adapter as a dedicated non-root
# account; root is the default and what production uses.
SCHEMA_BOOTSTRAP_OWNER="${SCHEMA_BOOTSTRAP_OWNER:-root}"

module_prerequisites_satisfied() {
  APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
    "${COMPOSE[@]}" run --rm --no-deps app sh -c \
    'MIGRATION_DATABASE_URL="$DATABASE_URL" python scripts/bootstrap_commercial_module_prereqs.py --verify-only' \
    >/dev/null 2>&1
}

# Every refusal below names the exact thing that was wrong. A bare `test` that
# exits 1 with no output is how the 2026-08-31 production repair became
# undiagnosable from its own log.
schema_bootstrap_credential_available() {
  local ok=0
  local authority userinfo

  if [[ -z "${SCHEMA_BOOTSTRAP_URL}" ]]; then
    echo "PREREQUISITE PREFLIGHT: SCHEMA_BOOTSTRAP_URL is not set (expected a passwordless postgresql:// URL for the dedicated bootstrap role)." >&2
    ok=1
  else
    # Inspect ONLY the userinfo. A naive `*:*@*` test also matches the scheme's
    # own colon in `postgresql://`, which would reject every correct URL.
    authority="${SCHEMA_BOOTSTRAP_URL#*://}"
    if [[ "${authority}" == *@* ]]; then
      userinfo="${authority%%@*}"
      if [[ "${userinfo}" == *:* ]]; then
        echo "PREREQUISITE PREFLIGHT: SCHEMA_BOOTSTRAP_URL carries an inline password; the credential must come from the pgpass file instead." >&2
        ok=1
      fi
    fi
  fi

  if [[ ! -e "${SCHEMA_BOOTSTRAP_PGPASS}" ]]; then
    echo "PREREQUISITE PREFLIGHT: credential file ${SCHEMA_BOOTSTRAP_PGPASS} does not exist." >&2
    ok=1
  elif [[ ! -f "${SCHEMA_BOOTSTRAP_PGPASS}" ]]; then
    echo "PREREQUISITE PREFLIGHT: ${SCHEMA_BOOTSTRAP_PGPASS} is not a regular file." >&2
    ok=1
  else
    local mode owner
    mode="$(stat -c '%a' "${SCHEMA_BOOTSTRAP_PGPASS}" 2>/dev/null || echo '?')"
    owner="$(stat -c '%U' "${SCHEMA_BOOTSTRAP_PGPASS}" 2>/dev/null || echo '?')"
    if [[ "${mode}" != "400" ]]; then
      echo "PREREQUISITE PREFLIGHT: ${SCHEMA_BOOTSTRAP_PGPASS} has mode ${mode}; expected 400 (libpq refuses a group- or world-readable pgpass)." >&2
      ok=1
    fi
    if [[ "${owner}" != "${SCHEMA_BOOTSTRAP_OWNER}" ]]; then
      echo "PREREQUISITE PREFLIGHT: ${SCHEMA_BOOTSTRAP_PGPASS} is owned by ${owner}; expected ${SCHEMA_BOOTSTRAP_OWNER}." >&2
      ok=1
    fi
    if [[ ! -s "${SCHEMA_BOOTSTRAP_PGPASS}" ]]; then
      echo "PREREQUISITE PREFLIGHT: ${SCHEMA_BOOTSTRAP_PGPASS} is empty." >&2
      ok=1
    fi
  fi

  return "${ok}"
}

run_database_prerequisite_bootstrap() {
  local bootstrap_url persisted_url

  # An elevated DSN persisted in the deploy directory's .env would arm
  # auto-repair on EVERY deployment, silently and permanently: `env_value`
  # greps that file, so the old
  #   "${BOOTSTRAP_DATABASE_URL:-$(env_value BOOTSTRAP_DATABASE_URL)}"
  # made "nobody has typed it into .env" the entire safety property. Refuse it
  # instead of depending on that. A one-off operator provisioning run passes
  # the DSN in the PROCESS environment, where it lasts exactly one invocation.
  persisted_url="$(env_value BOOTSTRAP_DATABASE_URL)"
  if [[ -n "${persisted_url}" ]]; then
    PREREQUISITE_OUTCOME="blocked"
    log "PREREQUISITE OUTCOME: blocked"
    cat >&2 <<'REFUSAL'
DEPLOY REFUSED: BOOTSTRAP_DATABASE_URL is set in the deploy directory's .env.

A persisted elevated DSN turns every deployment into an auto-repairing one,
with superuser-shaped rights, for as long as the line stays in the file. The
managed repair leg uses the least-privilege dotmac_schema_bootstrap credential
from a root-owned 0400 pgpass file instead, and that credential carries no
password in the URL at all.

Remove the line from .env. For a one-off elevated provisioning run, pass
BOOTSTRAP_DATABASE_URL in the environment of that single invocation.
REFUSAL
    exit 1
  fi

  # Operator provisioning path: an explicitly supplied elevated connection may
  # create cluster ROLES as well as schemas. Environment-only, so it is one
  # deliberate invocation rather than a standing configuration.
  bootstrap_url="${BOOTSTRAP_DATABASE_URL:-}"
  if [[ -n "${bootstrap_url}" ]]; then
    log "Repairing commercial module database prerequisites with the elevated bootstrap connection"
    BOOTSTRAP_DATABASE_URL="${bootstrap_url}" APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
      "${COMPOSE[@]}" run --rm --no-deps -e BOOTSTRAP_DATABASE_URL app \
      python scripts/bootstrap_commercial_module_prereqs.py --repair

    log "Repairing outbox dispatcher database prerequisites with the elevated bootstrap connection"
    BOOTSTRAP_DATABASE_URL="${bootstrap_url}" APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
      "${COMPOSE[@]}" run --rm --no-deps -e BOOTSTRAP_DATABASE_URL app \
      python scripts/bootstrap_outbox_dispatcher_roles.py --repair

    PREREQUISITE_OUTCOME="repaired"
    log "PREREQUISITE OUTCOME: repaired (elevated operator connection)"
    return 0
  fi

  log "Checking module database prerequisites with the restricted migration connection"
  if module_prerequisites_satisfied; then
    PREREQUISITE_OUTCOME="already_satisfied"
    log "PREREQUISITE OUTCOME: already_satisfied (contract holds; nothing written)"
    return 0
  fi

  log "Module database prerequisites are NOT satisfied; the managed repair leg is required"
  if ! schema_bootstrap_credential_available; then
    PREREQUISITE_OUTCOME="blocked"
    log "PREREQUISITE OUTCOME: blocked"
    cat >&2 <<'REFUSAL'
DEPLOY REFUSED: module database prerequisites need repair, but the dedicated
schema-bootstrap credential is unavailable (see PREREQUISITE PREFLIGHT lines
above for exactly which check failed).

This deploy will NOT continue to migrations. Refusing here is the point: the
previous behaviour was to carry on and fail the verification step with no
statement of what was missing or why nothing had been attempted.

To resolve, provision the dedicated role and its held credential on this host:
  - role dotmac_schema_bootstrap (NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOREPLICATION NOBYPASSRLS NOINHERIT), CONNECT + CREATE on this database
    only, member of dotmac_app without admin option
  - credential materialised root-owned 0400 at the pgpass path above
See docs/runbooks/PRODUCTION_DEPLOYMENT.md, "Module database prerequisites".
REFUSAL
    exit 1
  fi

  log "Repairing module schemas with the dedicated schema-bootstrap credential"
  APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
    "${COMPOSE[@]}" run --rm --no-deps --user 0:0 \
    -v "${SCHEMA_BOOTSTRAP_PGPASS}:${SCHEMA_BOOTSTRAP_PGPASS_MOUNT}:ro" \
    -e PGPASSFILE="${SCHEMA_BOOTSTRAP_PGPASS_MOUNT}" \
    -e BOOTSTRAP_DATABASE_URL="${SCHEMA_BOOTSTRAP_URL}" \
    app python scripts/bootstrap_commercial_module_prereqs.py --repair-schemas

  PREREQUISITE_OUTCOME="repaired"
  log "PREREQUISITE OUTCOME: repaired (managed schema-bootstrap credential)"
}

verify_database_prerequisites() {
  log "Verifying commercial module prerequisites with the restricted migration connection"
  APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
    "${COMPOSE[@]}" run --rm --no-deps app sh -c \
    'MIGRATION_DATABASE_URL="$DATABASE_URL" python scripts/bootstrap_commercial_module_prereqs.py --verify-only'

  log "Verifying outbox dispatcher prerequisites with the restricted migration connection"
  APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
    "${COMPOSE[@]}" run --rm --no-deps app sh -c \
    'MIGRATION_DATABASE_URL="$DATABASE_URL" python scripts/bootstrap_outbox_dispatcher_roles.py --verify-only'
}

# Print the database name from a postgres URL, or fail. Never echoes anything
# that is not a plain identifier, so a malformed URL cannot leak credentials.
# This is needed by database_heads during the early post-migration resume gate,
# before the deploy reaches the later pre-swap migration helpers.
database_name_from_url() {
  local rest="$1" name
  [[ "${rest}" == *://* ]] || return 1
  rest="${rest#*://}"
  rest="${rest%%\?*}"
  rest="${rest##*@}"
  [[ "${rest}" == */* ]] || return 1
  name="${rest#*/}"
  [[ "${name}" =~ ^[A-Za-z0-9_.-]+$ ]] || return 1
  printf '%s\n' "${name}"
}

database_heads() {
  local backup_db_user
  local backup_db_name
  local database_url

  backup_db_user="${DB_BACKUP_DB_USER:-$(env_value DB_BACKUP_DB_USER)}"
  backup_db_user="${backup_db_user:-postgres}"
  backup_db_name="${DB_BACKUP_DB_NAME:-$(env_value DB_BACKUP_DB_NAME)}"
  if [[ -z "${backup_db_name}" ]]; then
    database_url="$(env_value DATABASE_URL)"
    backup_db_name="$(database_name_from_url "${database_url}")" || backup_db_name=""
  fi
  if [[ -z "${backup_db_name}" ]]; then
    echo "RESUME POLICY REJECTED: cannot resolve database name for alembic head verification." >&2
    return 1
  fi
  # Bounded so a stuck lock or connection cannot hang the deploy: a timeout is
  # a non-zero exit, i.e. an unreadable read to every caller.
  docker exec -e PGCONNECT_TIMEOUT=5 \
    -e PGOPTIONS='-c lock_timeout=5s -c statement_timeout=15s' \
    "${DB_CONTAINER}" psql -X -A -t -U "${backup_db_user}" \
    -d "${backup_db_name}" \
    -c 'SELECT version_num FROM alembic_version ORDER BY version_num'
}

candidate_heads() {
  docker run --rm --entrypoint python "${IMAGE}" \
    -I -B -c 'import sys; sys.path.insert(0, "/app"); from scripts.ci.migrated_test_database import install_migration_graph_environment; install_migration_graph_environment(); from alembic.config import main; main(argv=sys.argv[1:])' \
    heads --resolve-dependencies \
    | sed -E 's/[[:space:]].*$//' \
    | grep -E '^[A-Za-z0-9_.-]+$'
}

verify_post_migration_resume() {
  local prior_run_id="${PRODUCTION_DEPLOY_RESUME_FAILED_RUN_ID:-}"
  local backup_path="${PRODUCTION_DEPLOY_RESUME_BACKUP_PATH:-}"
  local authorization_run_id="${PRODUCTION_DEPLOY_RESUME_AUTHORIZATION_RUN_ID:-}"
  local current_app_image
  local -a database_head_args=()
  local -a candidate_head_args=()
  local -a database_head_values=()
  local -a candidate_head_values=()
  local head

  [[ "${PRODUCTION_DEPLOY_RESUME_AFTER_MIGRATION:-0}" == "1" ]] || return 1
  [[ "${DEPLOYMENT_TARGET}" == "production" ]] || {
    echo "RESUME POLICY REJECTED: post-migration resume is production-only." >&2
    return 1
  }
  [[ "${prior_run_id}" =~ ^[0-9]+$ && "${prior_run_id}" -gt 0 ]] || {
    echo "RESUME POLICY REJECTED: prior failed deployment run ID is required." >&2
    return 1
  }
  [[ "${authorization_run_id}" =~ ^[0-9]+$ && "${authorization_run_id}" -gt 0 ]] || {
    echo "RESUME POLICY REJECTED: authorization run ID is required." >&2
    return 1
  }
  current_app_image="$(
    docker inspect "${APP_CONTAINER}" --format '{{.Config.Image}}' 2>/dev/null || true
  )"
  mapfile -t database_head_values < <(database_heads)
  mapfile -t candidate_head_values < <(candidate_heads)
  if ((${#database_head_values[@]} == 0 || ${#candidate_head_values[@]} == 0)); then
    echo "RESUME POLICY REJECTED: database or candidate Alembic heads could not be proven." >&2
    return 1
  fi
  for head in "${database_head_values[@]}"; do
    database_head_args+=(--database-head "${head}")
  done
  for head in "${candidate_head_values[@]}"; do
    candidate_head_args+=(--candidate-head "${head}")
  done
  run_repo_module scripts.deploy_resume_policy verify-post-migration \
    --prior-failed-run-id "${prior_run_id}" \
    --authorization-run-id "${authorization_run_id}" \
    --expected-authorization-run-id "${authorization_run_id}" \
    --backup-path "${backup_path}" \
    --candidate-digest "${TAG}" \
    --authorized-digest "${TAG}" \
    "${database_head_args[@]}" \
    "${candidate_head_args[@]}" \
    --current-app-image "${current_app_image}" \
    --previous-image "${PREV_IMAGE}" \
    --candidate-image "${IMAGE}"
}

# Deploy-integrity gate. The immutable image must not be shadowed by a host
# source bind-mount: a `/app/app` mount means a dev overlay (docker-compose.dev.yml,
# or an override carrying a working-tree mount) got layered on, so the RUNNING
# code is the host working tree — not the tag we just deployed. This is
# invisible to the health gate (host code can be perfectly healthy), so check it
# explicitly. `/app/uploads` and other named volumes are fine; only `/app/app`
# (the Python package) shadowing the image is the failure.
#
# This — not excluding docker-compose.override.yml — is what actually guards
# deploy integrity. An override may legitimately carry deployment config
# (profiles, host-specific storage); it is a source bind-mount that is fatal.
assert_no_source_mount() {
  local container="${1:-${APP_CONTAINER}}"
  local mounts
  mounts="$(docker inspect "${container}" \
    --format '{{range .Mounts}}{{println .Destination}}{{end}}' 2>/dev/null || true)"
  if grep -qx '/app/app' <<<"${mounts}"; then
    echo "DEPLOY INTEGRITY FAILURE: ${container} has a host bind-mount at /app/app —" >&2
    echo "the working tree is shadowing the image, so '${TAG:-?}' is NOT the running code." >&2
    echo "Cause: a dev overlay was loaded (a compose file mounting the working tree," >&2
    echo "or a bare 'docker compose up/restart' on the host). Fix on the host, then redeploy:" >&2
    echo "  ${COMPOSE[*]} up -d --force-recreate ${APP_SERVICES[*]}" >&2
    return 1
  fi
}

# --- Release-shipped service configuration ---------------------------------
#
# freeradius, vmagent and promtail read configuration bind-mounted from
# config/ in the deployment directory, and none of them re-reads it while
# running. A deploy never recreates them (see APP_SERVICES above), so a pulled
# config change used to reach the running process only when someone happened
# to restart the container by hand. On dotmac_erp that left vmagent on a
# two-week-old config after a `git pull`, merging production and staging
# metrics under one label (dotmac_erp PR #695).
#
# So after a release the deploy asks, per running service: did any checkout
# file this container bind-mounts change after the container started?
# (scripts/deploy_config_freshness.py explains why that, rather than a diff
# between two release revisions, is the right question on both hosts.) A
# changed service is restarted -- `docker compose restart` re-resolves every
# bind-mount source path, so it also picks up a file that `git` replaced with
# a new inode. A restart is NOT a recreate: the service definition (image,
# ports, volumes) is unchanged, and ADR-0014's published-port path still owns
# those.
#
# FreeRADIUS is subscriber authentication, so it gets more than a restart:
#   - A SIGHUP/`radmin hup` is not enough: FreeRADIUS 3 re-reads only HUP-safe
#     module configuration on HUP, not radiusd.conf, virtual servers
#     (sites-enabled), clients or dictionaries -- the files shipped here.
#   - The new configuration is validated with `freeradius -XC` in a throwaway
#     container from the same image and mounts, both before any database work
#     (fail the deploy early) and again immediately before the restart. A
#     rejected config is never restarted into: the running server keeps the
#     configuration it loaded.
#   - After the restart the container must stay up, and the synthetic RADIUS
#     probe (app/services/radius_probe.py) must answer at least as well as it
#     did before the restart.
# vmagent and promtail are observability agents: a failed restart is reported
# but never fails an otherwise healthy application deploy.
#
# See docs/runbooks/SERVICE_CONFIG_MOUNTS.md.
CONFIG_RESTART_SERVICES=(vmagent promtail freeradius)
FREERADIUS_SERVICE="freeradius"
# The service whose environment carries RADIUS_PROBE_SECRET/PASSWORD. It is a
# required service, so it is always present on a deploy host.
RADIUS_PROBE_SERVICE="${RADIUS_PROBE_SERVICE:-celery-worker}"
FREERADIUS_RESTART_TIMEOUT_SECONDS="${FREERADIUS_RESTART_TIMEOUT_SECONDS:-60}"
FREERADIUS_STABILITY_SECONDS="${FREERADIUS_STABILITY_SECONDS:-5}"
CONFIG_APPLY_FAILED=0

# Print the checkout bind-mount sources of a running container that changed
# after it started (one "changed|missing <path>" line each, nothing when it is
# current). Returns non-zero when that cannot be determined.
service_config_stale_sources() {
  local container="$1"
  local started_at
  local mounts
  local -a sources=()
  started_at="$(docker inspect "${container}" \
    --format '{{.State.StartedAt}}' 2>/dev/null)" || return 1
  [[ -n "${started_at}" ]] || return 1
  mounts="$(docker inspect "${container}" \
    --format '{{range .Mounts}}{{if eq .Type "bind"}}{{println .Source}}{{end}}{{end}}' \
    2>/dev/null)" || return 1
  mapfile -t sources <<<"${mounts}"
  run_repo_module scripts.deploy_config_freshness \
    --started-at "${started_at}" --within "${DEPLOY_DIR}" -- "${sources[@]}"
}

print_stale_sources() {
  local service="$1"
  local line
  while IFS= read -r line; do
    printf '  %s: %s\n' "${service}" "${line}"
  done <<<"$2"
}

validate_freeradius_config() {
  local output
  log "Validating FreeRADIUS configuration in a throwaway container (freeradius -XC)"
  # `compose run` creates a new container from the same image, environment and
  # bind mounts as the service, without publishing its ports, so it reads the
  # checkout's current files while the running server keeps serving.
  # -X masks secret-typed values (client secrets, SQL password) in its output.
  if output="$("${COMPOSE[@]}" run --rm --no-deps -T "${FREERADIUS_SERVICE}" \
    freeradius -XC 2>&1)"; then
    echo "  FreeRADIUS accepted the checkout configuration."
    return 0
  fi
  echo "FREERADIUS CONFIG REJECTED: freeradius -XC failed against the checkout configuration." >&2
  echo "The running server was not restarted and keeps the configuration it loaded." >&2
  echo "Last lines of the validation output:" >&2
  tail -n 40 <<<"${output}" >&2
  return 1
}

# Outcome of one synthetic Access-Request: accept | reject | timeout | error |
# unconfigured | unavailable.
radius_probe_outcome() {
  local outcome
  outcome="$("${COMPOSE[@]}" exec -T "${RADIUS_PROBE_SERVICE}" python -c \
    'from app.services.radius_probe import run_configured_probe
_fields, result = run_configured_probe()
print(result.outcome if result is not None else "unconfigured")' \
    2>/dev/null | tail -n 1)" || outcome=""
  case "${outcome}" in
    accept | reject | timeout | error | unconfigured) printf '%s\n' "${outcome}" ;;
    *) printf 'unavailable\n' ;;
  esac
}

# Prove the restarted server stays up and answers at least as well as it did
# before the restart. A probe that was not answering beforehand (unconfigured,
# or no response) cannot prove more than liveness, and must not turn a config
# apply into a failure on its own.
wait_for_freeradius_after_restart() {
  local baseline="$1"
  local deadline=$((SECONDS + FREERADIUS_RESTART_TIMEOUT_SECONDS))
  local container state restart_count outcome="" stable_since="" stable_count=""
  while true; do
    container="$(service_container_id "${FREERADIUS_SERVICE}")" || container=""
    state=""
    restart_count=""
    if [[ -n "${container}" ]]; then
      state="$(docker inspect "${container}" --format '{{.State.Status}}' 2>/dev/null || true)"
      restart_count="$(docker inspect "${container}" --format '{{.RestartCount}}' 2>/dev/null || true)"
    fi
    # A server that refuses its config exits, and the unless-stopped policy
    # restarts it: RestartCount moves. Require it to hold still for the
    # stability window rather than assuming any absolute value, because the
    # count can carry history from before this restart.
    if [[ "${state}" == "running" && -n "${stable_since}" \
      && "${restart_count}" != "${stable_count}" ]]; then
      stable_since=""
    fi
    if [[ "${state}" == "running" ]]; then
      if [[ -z "${stable_since}" ]]; then
        stable_since="${SECONDS}"
        stable_count="${restart_count}"
      fi
      if ((SECONDS - stable_since >= FREERADIUS_STABILITY_SECONDS)); then
        case "${baseline}" in
          accept)
            outcome="$(radius_probe_outcome)"
            [[ "${outcome}" == "accept" ]] && break
            ;;
          reject)
            outcome="$(radius_probe_outcome)"
            [[ "${outcome}" == "accept" || "${outcome}" == "reject" ]] && break
            ;;
          *)
            echo "  WARNING: the RADIUS probe was '${baseline}' before the restart, so only liveness is proven." >&2
            break
            ;;
        esac
      fi
    else
      stable_since=""
    fi
    if ((SECONDS >= deadline)); then
      echo "FREERADIUS RESTART HEALTH FAILED: state=${state:-absent} restart_count=${restart_count:-unknown} probe=${outcome:-not run} (before restart: ${baseline})." >&2
      echo "Subscriber authentication may be down. The validated config is already loaded; inspect now:" >&2
      echo "  ${COMPOSE[*]} logs --tail 200 ${FREERADIUS_SERVICE}" >&2
      echo "  ${COMPOSE[*]} run --rm --no-deps ${FREERADIUS_SERVICE} freeradius -X" >&2
      return 1
    fi
    sleep 2
  done
  echo "  freeradius is running${outcome:+; probe ${outcome}}."
}

restart_freeradius_with_validation() {
  local baseline
  if ! validate_freeradius_config; then
    return 1
  fi
  baseline="$(radius_probe_outcome)"
  log "Restarting freeradius to load the validated configuration (probe before restart: ${baseline})"
  # Authentication and accounting pause for the stop/start, normally a few
  # seconds. NAS clients retransmit across it.
  if ! "${COMPOSE[@]}" restart "${FREERADIUS_SERVICE}"; then
    echo "FREERADIUS RESTART FAILED: docker compose restart ${FREERADIUS_SERVICE} returned non-zero." >&2
    wait_for_freeradius_after_restart "${baseline}" || true
    return 1
  fi
  wait_for_freeradius_after_restart "${baseline}"
}

# Before any database work: a FreeRADIUS config change that the server would
# refuse fails the deploy here, while nothing has been touched.
preflight_service_config() {
  local container stale
  service_is_declared "${FREERADIUS_SERVICE}" || return 0
  container="$(service_container_id "${FREERADIUS_SERVICE}")" || container=""
  [[ -n "${container}" ]] || return 0
  if ! stale="$(service_config_stale_sources "${container}")"; then
    echo "  WARNING: cannot determine whether freeradius config changed; it is re-checked after the release." >&2
    return 0
  fi
  if [[ -z "${stale}" ]]; then
    echo "  freeradius config unchanged since the container started."
    return 0
  fi
  print_stale_sources "${FREERADIUS_SERVICE}" "${stale}"
  validate_freeradius_config
}

# After a healthy release: restart only the services whose mounted checkout
# config changed after they started. Sets CONFIG_APPLY_FAILED=1 when a
# FreeRADIUS change could not be applied safely.
apply_service_config_changes() {
  local service container stale
  for service in "${CONFIG_RESTART_SERVICES[@]}"; do
    if ! service_is_declared "${service}"; then
      continue
    fi
    container="$(service_container_id "${service}")" || container=""
    if [[ -z "${container}" ]]; then
      echo "  ${service}: not running; nothing to apply."
      continue
    fi
    if ! stale="$(service_config_stale_sources "${container}")"; then
      echo "  WARNING: ${service}: cannot determine whether its config changed; not restarted." >&2
      continue
    fi
    if [[ -z "${stale}" ]]; then
      echo "  ${service}: config unchanged since the container started."
      continue
    fi
    print_stale_sources "${service}" "${stale}"
    if [[ "${service}" == "${FREERADIUS_SERVICE}" ]]; then
      if ! restart_freeradius_with_validation; then
        CONFIG_APPLY_FAILED=1
      fi
      continue
    fi
    echo "  Restarting ${service} to load its changed config..."
    if ! "${COMPOSE[@]}" restart "${service}"; then
      echo "  WARNING: restarting ${service} failed; it may still run the previous config." >&2
    fi
  done
}

# Compose file set.
#
# The base Compose contract belongs to the authorized release checkout, not the
# persistent host deployment directory. Production intentionally separates
# those locations: REPO_DIR is the exact Actions checkout while DEPLOY_DIR owns
# only host state such as .env and an optional host-specific override. Reading
# the base file from DEPLOY_DIR let a stale checkout omit a newly required
# worker even while the host ran the newer image.
#
# This used to be `-f docker-compose.yml` alone, which silently diverges from
# every bare `docker compose` command on the host: Compose auto-loads
# docker-compose.override.yml, this script did not. On a host whose override
# carries real deployment configuration that divergence is not cosmetic --
# staging's override gates `celery-beat` behind a `production-scheduler`
# profile (added after a staging scheduler wrote to production) and pins
# staging-only object storage. Deploying without it resurrects both.
#
# Production DOES have an override file: /root/dotmac_sub/docker-compose.override.yml,
# 21 services, measured 2026-08-31. (An earlier version of this comment said it
# had none, which was wrong and is the sort of thing people reason from.) It
# tunes resources and `command` for postgres-local, redis-local and the workers,
# and publishes team-inbox-smtp on 127.0.0.1:18001. It declares no `ports:` for
# postgres-local, so the base file's binding is what applies there.
# Set IGNORE_COMPOSE_OVERRIDE=1 to force single-file behaviour.
RELEASE_COMPOSE_FILE="${REPO_DIR}/docker-compose.yml"
HOST_COMPOSE_OVERRIDE="${DEPLOY_DIR}/docker-compose.override.yml"
if [[ ! -f "${RELEASE_COMPOSE_FILE}" ]]; then
  echo "DEPLOY CONFIG FAILURE: authorized release has no ${RELEASE_COMPOSE_FILE}." >&2
  exit 1
fi
COMPOSE=(docker compose --project-directory "${DEPLOY_DIR}" \
  --env-file "${DEPLOY_DIR}/.env" -f "${RELEASE_COMPOSE_FILE}")
COMPOSE_FILES_DESC="${RELEASE_COMPOSE_FILE}"
if [[ "${IGNORE_COMPOSE_OVERRIDE:-0}" != "1" && -f "${HOST_COMPOSE_OVERRIDE}" ]]; then
  COMPOSE+=(-f "${HOST_COMPOSE_OVERRIDE}")
  COMPOSE_FILES_DESC+=" + ${HOST_COMPOSE_OVERRIDE}"
fi

# Restrict optional services this deploy touches to the ones the resolved
# Compose config actually declares. Naming a profile-gated service explicitly
# ACTIVATES its profile, so a hardcoded `up -d ... celery-beat` would create
# the very scheduler the staging override exists to suppress.
#
# Worker services are not optional on a production host. Dropping a missing
# queue consumer here lets beat keep publishing tasks to an unconsumed Redis
# queue while the deploy still reports green.
DECLARED_SERVICES=""
load_declared_services() {
  if ! DECLARED_SERVICES="$("${COMPOSE[@]}" config --services 2>&1)"; then
    echo "DEPLOY CONFIG FAILURE: cannot resolve Compose services from ${COMPOSE_FILES_DESC}." >&2
    printf '%s\n' "${DECLARED_SERVICES}" >&2
    return 1
  fi
  DECLARED_SERVICES=" $(tr '\n' ' ' <<<"${DECLARED_SERVICES}") "
}

service_is_declared() {
  [[ "${DECLARED_SERVICES}" == *" $1 "* ]]
}

# Prints the subset of "$@" that the resolved Compose config declares.
declared_subset() {
  local service
  for service in "$@"; do
    if service_is_declared "${service}"; then
      printf '%s\n' "${service}"
    fi
  done
}

assert_required_services_declared() {
  local service
  local missing=()
  for service in "$@"; do
    if ! service_is_declared "${service}"; then
      missing+=("${service}")
    fi
  done
  if ((${#missing[@]} > 0)); then
    echo "DEPLOY CONFIG FAILURE: required Compose service(s) missing from ${COMPOSE_FILES_DESC}: ${missing[*]}" >&2
    return 1
  fi
}

set_env_value() {
  local key="$1"
  local value="$2"
  local backup_suffix=".deploy-$$.bak"
  if grep -q "^${key}=" .env; then
    sed -i"${backup_suffix}" "s|^${key}=.*|${key}=${value}|" .env || return 1
    rm -f -- ".env${backup_suffix}" || return 1
  else
    printf '%s=%s\n' "${key}" "${value}" >> .env
  fi
}

restore_env_value() {
  local key="$1"
  local was_present="$2"
  local value="$3"
  if [[ "${was_present}" == "1" ]]; then
    set_env_value "${key}" "${value}"
  else
    local backup_suffix=".deploy-$$.bak"
    sed -i"${backup_suffix}" "/^${key}=/d" .env || return 1
    rm -f -- ".env${backup_suffix}" || return 1
  fi
}

pinned_image() { env_value APP_IMAGE; }
pinned_git_sha() { env_value GIT_SHA; }

image_revision() {
  docker image inspect "$1" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
}

image_source_tree() {
  docker image inspect "$1" \
    --format '{{index .Config.Labels "io.dotmac.release.source-tree"}}'
}

validate_release_source_tree() {
  local image="$1"
  local image_tree="$2"
  local release_tree
  if [[ ! "${image_tree}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "DEPLOY INTEGRITY FAILURE: ${image} has no full source-tree label." >&2
    return 1
  fi
  if ! release_tree="$(git -C "${REPO_DIR}" rev-parse 'HEAD^{tree}' 2>/dev/null)"; then
    echo "DEPLOY INTEGRITY FAILURE: ${REPO_DIR} is not an authorized Git checkout." >&2
    return 1
  fi
  if [[ "${release_tree}" != "${image_tree}" ]]; then
    echo "DEPLOY INTEGRITY FAILURE: release Compose tree ${release_tree} does not match image source tree ${image_tree}." >&2
    return 1
  fi
}

validate_image_revision() {
  local image="$1"
  local tag="$2"
  local revision="$3"
  local repo_digests
  local tag_sha
  if [[ ! "${revision}" =~ ^[0-9a-f]{40}$ ]]; then
    echo "IMAGE INTEGRITY FAILURE: ${image} has no full OCI revision label." >&2
    return 1
  fi
  if [[ "${tag}" == sha-* ]]; then
    tag_sha="${tag#sha-}"
    if [[ "${revision:0:${#tag_sha}}" != "${tag_sha}" ]]; then
      echo "IMAGE INTEGRITY FAILURE: tag ${tag} does not match OCI revision ${revision}." >&2
      return 1
    fi
  fi
  if [[ "${tag}" == sha256:* ]]; then
    repo_digests="$(
      docker image inspect "${image}" \
        --format '{{range .RepoDigests}}{{println .}}{{end}}'
    )"
    if ! grep -Fxq "${image}" <<<"${repo_digests}"; then
      echo "IMAGE INTEGRITY FAILURE: pulled image is not pinned to ${image}." >&2
      return 1
    fi
  fi
}

if [[ "${1:-}" == "--status" ]]; then
  echo "pinned:  $(pinned_image)"
  echo "git sha: $(pinned_git_sha)"
  echo "running: $(docker inspect "${APP_CONTAINER}" --format '{{.Config.Image}}' 2>/dev/null || echo 'not running')"
  exit 0
fi

TAG="${1:?usage: deploy.sh <image-tag-or-digest>, e.g. sha-abc1234 or sha256:<64-hex> (or --status)}"
if [[ "${TAG}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  IMAGE="${IMAGE_REPO}@${TAG}"
elif [[ "${TAG}" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
  IMAGE="${IMAGE_REPO}:${TAG}"
else
  echo "IMAGE INTEGRITY FAILURE: image selector must be a valid tag or sha256 digest." >&2
  exit 1
fi
PREV_IMAGE="$(pinned_image)"
PREV_IMAGE_PRESENT="$(grep -q '^APP_IMAGE=' .env && printf 1 || printf 0)"
PREV_GIT_SHA="$(pinned_git_sha)"
PREV_GIT_SHA_PRESENT="$(grep -q '^GIT_SHA=' .env && printf 1 || printf 0)"

# The deployment environment determines the only branch whose GitHub-owned
# decision can authorize this host. An operator cannot point production at a
# green dev-only build, and an ambiguous host fails before any external query
# or database backup.
case "$(env_value APP_ENV):$(env_value SERVER_NAME)" in
  production:dotmac-sub-prod)
    GITHUB_RELEASE_BRANCH="main"
    DEPLOYMENT_TARGET="production"
    if [[ "${SKIP_BACKUP:-0}" == "1" ]]; then
      echo "BACKUP POLICY REJECTED: production does not accept SKIP_BACKUP=1." >&2
      exit 1
    fi
    ;;
  staging:dotmac-sub-staging)
    GITHUB_RELEASE_BRANCH="main"
    DEPLOYMENT_TARGET="staging"
    if [[ "${SKIP_BACKUP:-0}" == "1" ]]; then
      echo "BACKUP POLICY REJECTED: use the verified staging adapter." >&2
      exit 1
    fi
    ;;
  *)
    echo "GITHUB RELEASE GATE REJECTED: APP_ENV and SERVER_NAME do not identify an approved deployment host." >&2
    exit 1
    ;;
esac

# A public repository needs no token. Private/restricted installations inject
# a read-only token through the process environment or the host's untracked
# .env. Never echo it or place it on the command line.
if [[ -z "${GITHUB_DEPLOY_GATE_TOKEN:-}" ]]; then
  GITHUB_DEPLOY_GATE_TOKEN="$(env_value GITHUB_DEPLOY_GATE_TOKEN)"
fi
export GITHUB_DEPLOY_GATE_TOKEN

if [[ "${IMAGE}" == "${PREV_IMAGE}" ]]; then
  log "Image ${IMAGE} is already pinned — re-running deploy steps idempotently."
fi

log "Deploying ${IMAGE} (currently pinned: ${PREV_IMAGE:-none})"
log "Compose files: ${COMPOSE_FILES_DESC}"

# Cheap, local preconditions first: this gate must be able to refuse without
# having touched anything, including read-only queries.
log "Verifying Nginx warm-handoff contract"
assert_proxy_handoff_contract
log "Verifying warm-candidate port availability"
assert_candidate_port_available

log "Resolving declared Compose services"
load_declared_services
assert_required_services_declared app "${CELERY_WORKER_SERVICES[@]}"
mapfile -t APP_SERVICES < <(declared_subset "${APP_SERVICES[@]}")
if ((${#APP_SERVICES[@]} == 0)); then
  echo "DEPLOY CONFIG FAILURE: none of the app services are declared in ${COMPOSE_FILES_DESC}." >&2
  exit 1
fi
log "Services this deploy will recreate: ${APP_SERVICES[*]}"

log "Verifying image exists on registry"
docker manifest inspect "${IMAGE}" >/dev/null

# Pulling changes only the host's local image cache. The release SHA must be
# known and accepted by GitHub before the backup or any database/application
# mutation begins.
log "Pulling image"
docker pull "${IMAGE}"

log "Verifying image release metadata"
if ! FULL_SHA="$(image_revision "${IMAGE}")"; then
  echo "IMAGE INTEGRITY FAILURE: could not inspect ${IMAGE}." >&2
  exit 1
fi
if ! validate_image_revision "${IMAGE}" "${TAG}" "${FULL_SHA}"; then
  exit 1
fi
if ! IMAGE_SOURCE_TREE="$(image_source_tree "${IMAGE}")"; then
  echo "DEPLOY INTEGRITY FAILURE: could not inspect ${IMAGE} source tree." >&2
  exit 1
fi
if ! validate_release_source_tree "${IMAGE}" "${IMAGE_SOURCE_TREE}"; then
  exit 1
fi

if ! command -v "${PYTHON_BIN}" >/dev/null; then
  echo "GITHUB RELEASE GATE REJECTED: ${PYTHON_BIN} is unavailable on the deployment host." >&2
  exit 1
fi
log "Verifying exact ${GITHUB_RELEASE_BRANCH} revision passed GitHub-hosted CI"
GITHUB_RELEASE_REVISION="${FULL_SHA}"
if [[ "${DEPLOYMENT_TARGET}" == "production" ]]; then
  if [[ ! "${TAG}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo "PRODUCTION RELEASE GATE REJECTED: production requires an exact OCI digest." >&2
    exit 1
  fi
  if [[ -z "${PRODUCTION_RELEASE_EVIDENCE:-}" || ! -f "${PRODUCTION_RELEASE_EVIDENCE}" ]]; then
    echo "PRODUCTION RELEASE GATE REJECTED: typed production authorization is required." >&2
    exit 1
  fi
  PRODUCTION_VERIFY_ARGS=(
    verify-production
    --path "${PRODUCTION_RELEASE_EVIDENCE}"
    --expected-source-revision "${FULL_SHA}"
    --expected-image-digest "${TAG}"
  )
  if [[ -n "${PRODUCTION_DEPLOY_RESUME_AUTHORIZATION_RUN_ID:-}" ]]; then
    PRODUCTION_VERIFY_ARGS+=(
      --expected-authorization-run-id "${PRODUCTION_DEPLOY_RESUME_AUTHORIZATION_RUN_ID}"
    )
  fi
  if ! GITHUB_RELEASE_REVISION="$(
    run_repo_module scripts.release_candidate_evidence "${PRODUCTION_VERIFY_ARGS[@]}"
  )"; then
    echo "PRODUCTION RELEASE GATE REJECTED: authorization evidence did not match the image." >&2
    exit 1
  fi
fi
"${PYTHON_BIN}" "${REPO_DIR}/scripts/verify_github_release.py" \
  --repository "${GITHUB_RELEASE_REPOSITORY}" \
  --revision "${GITHUB_RELEASE_REVISION}" \
  --branch "${GITHUB_RELEASE_BRANCH}"

log "Checking release-shipped FreeRADIUS configuration"
if ! preflight_service_config; then
  echo "DEPLOY REFUSED before any database work: fix the FreeRADIUS configuration and redeploy." >&2
  exit 1
fi

preflight_feature_secret_requirements() {
  local phase="${1:-before_migrations}"
  local args=(--component app)
  local compose_args=(run --rm --no-deps --entrypoint /opt/venv/bin/python)
  if [[ "${phase}" == "after_migrations" ]]; then
    args+=(--require-installed-schema)
  elif [[ -n "${PRODUCTION_BOOTSTRAP_AUTHORIZATION_FILE:-}" ]]; then
    [[ "${DEPLOYMENT_TARGET}" == "production" && -f "${PRODUCTION_BOOTSTRAP_AUTHORIZATION_FILE}" ]] || return 1
    compose_args+=(-v "${PRODUCTION_BOOTSTRAP_AUTHORIZATION_FILE}:/run/deployment-bootstrap-authorization.json:ro")
    args+=(--bootstrap-authorization /run/deployment-bootstrap-authorization.json
      --candidate-revision "${FULL_SHA}" --target-server dotmac-sub-prod)
  fi
  if ! APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
    "${COMPOSE[@]}" "${compose_args[@]}" app \
    -m scripts.setup.verify_deployment_secret_requirements "${args[@]}" 2>/dev/null; then
    echo "DEPLOY REFUSED: declared deployment secret requirements are not ready." >&2
    return 1
  fi
}

preflight_deployment_secrets() {
  local component plan
  local boot_components=(app "${CELERY_WORKER_SERVICES[@]}")
  local plan_args=(--image "${IMAGE}")
  if service_is_declared "${CELERY_BEAT_SERVICE}"; then
    boot_components+=("${CELERY_BEAT_SERVICE}")
  fi
  for component in "${boot_components[@]}"; do
    plan_args+=(--component "${component}")
  done
  # Resolved Compose JSON contains host secrets. Consume it only in the typed
  # parser; neither raw config nor host-controlled errors reach deploy output.
  if ! plan="$(
    APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
      "${COMPOSE[@]}" config --format json 2>/dev/null |
      run_repo_module scripts.deployment_secret_readiness "${plan_args[@]}" 2>/dev/null
  )"; then
    echo "DEPLOY REFUSED: candidate secret component graph is unavailable or mismatched." >&2
    return 1
  fi
  while IFS= read -r component; do
    [[ -n "${component}" ]] || return 1
    log "Checking candidate boot-secret readiness for ${component}"
    # Bypass service startup commands: this one-off only evaluates readiness.
    # --no-deps never starts backing services or a profile-gated scheduler.
    if ! APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
      "${COMPOSE[@]}" run --rm --no-deps --entrypoint /opt/venv/bin/python "${component}" \
      -m scripts.setup.verify_openbao_boot_secrets --component "${component}" 2>/dev/null; then
      echo "DEPLOY REFUSED: candidate boot secrets are not ready for ${component}." >&2
      return 1
    fi
  done <<<"${plan}"
  log "Checking declared feature and enabled-integration secret requirements"
  preflight_feature_secret_requirements before_migrations
}

log "Verifying candidate secret readiness before database work"
if ! preflight_deployment_secrets; then
  echo "DEPLOY REFUSED before database bootstrap, backup, migrations or service replacement." >&2
  exit 1
fi

run_database_prerequisite_bootstrap
verify_database_prerequisites

# If this script is killed mid-backup -- an SSH session dropping is enough --
# the pg_dump it started does NOT die with it. It keeps running, and the next
# deploy attempt piles another one on top. Take the whole child tree down.
cleanup_children() {
  local rc=$?
  if [[ -n "${BACKUP_PID:-}" ]] && kill -0 "${BACKUP_PID}" 2>/dev/null; then
    kill -TERM "${BACKUP_PID}" 2>/dev/null || true
  fi
  # pg_dump runs inside the DB container via `docker exec`; it survives the
  # exec client, so signal it there too.
  docker exec "${DB_CONTAINER}" sh -c "pkill -f pg_dump" >/dev/null 2>&1 || true
  return $rc
}
trap 'cleanup_children; echo "Deploy interrupted -- backup child terminated, nothing pinned or migrated" >&2; exit 130' INT TERM HUP

BACKUP_MODE="required"
if [[ "${DEPLOYMENT_TARGET}" == "staging" ]]; then
  if [[ "${DEPLOY_BACKUP_MODE:-}" != "skip_staging" ]]; then
    echo "BACKUP POLICY REJECTED: staging deployments must use scripts/deploy_staging.sh." >&2
    exit 1
  fi
  BACKUP_MODE="skip_staging"
elif [[ -n "${PRODUCTION_BACKUP_DECISION_FILE:-}" ]]; then
  if ! BACKUP_MODE="$(
    run_repo_module scripts.release_backup_policy \
      verify-production-decision --path "${PRODUCTION_BACKUP_DECISION_FILE}"
  )"; then
    echo "BACKUP POLICY REJECTED: production hotfix evidence is invalid." >&2
    exit 1
  fi
elif [[ "${PRODUCTION_DEPLOY_RESUME_AFTER_MIGRATION:-0}" == "1" ]]; then
  if ! BACKUP_MODE="$(verify_post_migration_resume)"; then
    echo "BACKUP POLICY REJECTED: production post-migration resume evidence is invalid." >&2
    exit 1
  fi
fi

if [[ "${BACKUP_MODE}" == "required" ]]; then
  log "Backing up database before migrations"
  # db_backup.sh reads .env from ROOT_DIR, defaulting to its own parent — the
  # code checkout. That is only correct where the deploy dir and the repo are
  # the same directory. On the runner-based production path they are not:
  # REPO_DIR is the ephemeral Actions workspace and DEPLOY_DIR is the pinned
  # host directory that actually holds .env, so the backup aborted with
  # "Missing <workspace>/.env" and no production deploy ever completed.
  run_database_backup() {
    DB_BACKUP_BASENAME="${DB_BACKUP_BASENAME:-dotmac_sub}" \
      DB_BACKUP_DIR="${DB_BACKUP_DIR:-/var/backups/dotmac_sub/deployments}" \
      DB_BACKUP_LEGACY_DIR="${DB_BACKUP_LEGACY_DIR:-/var/backups/dotmac_sub}" \
      DB_BACKUP_RETENTION_PREFIX="${DB_BACKUP_RETENTION_PREFIX:-dotmac_sub_run_}" \
      DB_BACKUP_RETENTION_COUNT="${DB_BACKUP_RETENTION_COUNT:-5}" \
      ROOT_DIR="${DEPLOY_DIR}" bash "${REPO_DIR}/scripts/db_backup.sh"
  }
  if [[ "${DEPLOYMENT_TARGET}" == "production" && -n "${DEPLOY_RUN_ID:-}" && -z "${DB_BACKUP_BASENAME:-}" ]]; then
    DB_BACKUP_BASENAME="dotmac_sub_run_${DEPLOY_RUN_ID}" run_database_backup &
  else
    run_database_backup &
  fi
  BACKUP_PID=$!
  wait "${BACKUP_PID}"
  BACKUP_PID=""
elif [[ "${BACKUP_MODE}" == "skip_staging" ]]; then
  log "Skipping staging database backup under the verified staging host policy"
elif [[ "${BACKUP_MODE}" == "skip_production_hotfix" ]]; then
  log "Skipping production backup under verified no-migration hotfix evidence"
elif [[ "${BACKUP_MODE}" == "skip_production_post_migration_resume" ]]; then
  log "Skipping production backup under verified post-migration resume evidence"
else
  echo "BACKUP POLICY REJECTED: unrecognized backup mode ${BACKUP_MODE}." >&2
  exit 1
fi

repin_prev() {
  restore_env_value APP_IMAGE "${PREV_IMAGE_PRESENT}" "${PREV_IMAGE}" || return 1
  restore_env_value GIT_SHA "${PREV_GIT_SHA_PRESENT}" "${PREV_GIT_SHA}" || return 1
}
previous_image_rollback_allowed() {
  # Execute both checks from the candidate image and its runtime DB connection,
  # before changing either pin or recreating a container from the old image.
  # Each floor is permanent after installation; rollback needs proof of both.
  APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
    "${COMPOSE[@]}" run --rm --no-deps app \
    python -m scripts.verify_payment_email_rollback \
    && APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
      "${COMPOSE[@]}" run --rm --no-deps app \
      python -m scripts.verify_payments_intent_rollback
}
# A failure on or after `up -d` (below) may already have replaced/stopped the
# previous containers — repinning .env alone does not bring anything back up.
# Recreate on the restored pin too, then prove restored health. This function
# runs inside `if`, so every failure must be checked explicitly: Bash disables
# errexit for the entire function in that context.
ROLLBACK_OUTCOME="unknown"
restore_prev() {
  if [[ "${DOTMAC_MAINTENANCE_CONTEXT_FD:-}" == 7 ]]; then
    ROLLBACK_OUTCOME="maintenance_forward_only"
    echo "DEPLOY RECOVERY REFUSED: maintenance repair keeps the incumbent stopped and repairs forward; no legacy pin or container restart is permitted." >&2
    return 1
  fi
  if ! previous_image_rollback_allowed; then
    ROLLBACK_OUTCOME="floor_refused"
    echo "DEPLOY ROLLBACK REFUSED: a permanent Payments floor is installed or unproved; retain candidate and repair forward." >&2
    return 1
  fi
  ROLLBACK_OUTCOME="restore_failed"
  if ! repin_prev; then
    echo "DEPLOY RESTORE FAILED: previous-image pin is incomplete; inspect APP_IMAGE/GIT_SHA and repair forward." >&2
    return 1
  fi
  if [[ "${PRIMARY_REPLACED}" == "1" && -n "${PREV_IMAGE}" ]]; then
    if ! "${COMPOSE[@]}" up -d "${APP_SERVICES[@]}"; then
      echo "DEPLOY RESTORE FAILED: previous-image recreation failed; retain candidate and repair forward." >&2
      return 1
    fi
    if ! wait_for_health "${HEALTH_URL}" "Rolled-back primary"; then
      echo "DEPLOY RESTORE FAILED: previous image is not healthy; retain candidate and repair forward." >&2
      return 1
    fi
  fi
  cleanup_candidate
  ROLLBACK_OUTCOME="restored"
}
report_rollback_outcome() {
  if [[ "${ROLLBACK_OUTCOME}" == "maintenance_forward_only" ]]; then
    echo "Maintenance repair remains stopped with its candidate pin retained; resolve forward under the trusted deployment lock." >&2
  elif [[ "${ROLLBACK_OUTCOME}" == "floor_refused" ]]; then
    echo "Automatic image rollback refused; deployment pin left as-is and candidate retained where available. Repair forward." >&2
  else
    echo "Previous-image restore incomplete; inspect APP_IMAGE/GIT_SHA, candidate retained where available. Repair forward." >&2
  fi
}
trap 'if restore_prev; then echo "Deploy FAILED — previous image restored and healthy" >&2; else report_rollback_outcome; fi' ERR

# From here on APP_IMAGE may already be pinned, so an interrupt must restore it
# too -- not just terminate the backup child. Migrations are NOT reverted;
# installed payment-email floor forbids old-image restoration.
install_deploy_interrupt_trap() {
  trap 'cleanup_children; if restore_prev; then echo "Deploy interrupted — previous image restored and healthy" >&2; else report_rollback_outcome; fi; exit 130' INT TERM HUP
}
install_deploy_interrupt_trap

log "Verifying pre-migration service-extension identity state"
APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
  "${COMPOSE[@]}" run --rm --no-deps app \
  python -m scripts.migration.reconcile_service_extension_duplicates --check

log "Verifying five native service-team pointers and legacy-source retirement"
APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
  "${COMPOSE[@]}" run --rm --no-deps app \
  python -m scripts.migration.retire_legacy_service_team_sources --check

# --- Pre-swap pin and migration ---------------------------------------------
# The compose one-off migration container reads APP_IMAGE/GIT_SHA from .env, so
# the candidate pin must be written first. The OLD app and workers keep serving
# throughout (PRIMARY_REPLACED=0, no candidate started). If the migration fails
# here nothing new ever served. That is NOT a rollback: restore_prev (the
# payment-email floor guard) is deliberately not invoked and not bypassed.
#
# Alembic normally runs in one transaction, but migrations that use
# autocommit_block commit earlier revisions, so "nothing committed" is MEASURED,
# never assumed: the alembic_version heads are read before and after. The pin is
# put back ONLY when both reads succeed and are equal; otherwise the schema may
# have advanced, the pin is left untouched and the operator must repair forward.
read_alembic_heads() {
  local out url url_db target_db
  # Alembic connects with the DATABASE_URL the app container receives from
  # .env (settings.database_url). The heads query goes through the DB
  # container with DB_BACKUP_DB_NAME, so prove both name the same database;
  # otherwise two equal reads would prove nothing. Mismatch = unreadable.
  # Only validated database names are printed, never the URL.
  url="$(env_value DATABASE_URL)"
  if [[ -n "${DATABASE_URL+x}" && "${DATABASE_URL}" != "${url}" ]]; then
    echo "ALEMBIC HEADS UNREADABLE: DATABASE_URL in the environment differs from .env." >&2
    return 1
  fi
  if ! url_db="$(database_name_from_url "${url}")"; then
    echo "ALEMBIC HEADS UNREADABLE: database name unparseable from DATABASE_URL." >&2
    return 1
  fi
  target_db="${DB_BACKUP_DB_NAME:-$(env_value DB_BACKUP_DB_NAME)}"
  if [[ -n "${target_db}" && ! "${target_db}" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "ALEMBIC HEADS UNREADABLE: DB_BACKUP_DB_NAME is not a plain database name." >&2
    return 1
  fi
  if [[ -n "${target_db}" && "${target_db}" != "${url_db}" ]]; then
    echo "ALEMBIC HEADS UNREADABLE: heads query targets database '${target_db}' but alembic uses '${url_db}'." >&2
    return 1
  fi
  out="$(database_heads 2>/dev/null)" || return 1
  [[ -n "${out}" ]] || return 1
  printf '%s\n' "${out}"
}

# The workflow binds this absolute deadline at job start. Never treat time spent
# on the backup as a fresh cutover allowance. Clock-skew allowance is deducted
# before comparing the remaining interval with the explicit cutover reserve.
require_production_cutover_budget() {
  local deadline now remaining required="${1:-}"
  deadline="${PRODUCTION_DEPLOY_DEADLINE_EPOCH:-}"
  if [[ ! "${deadline}" =~ ^[0-9]{10}$ ]]; then
    echo "DEPLOY REFUSED: trusted production job deadline is absent or malformed." >&2
    return 1
  fi
  now="$(date -u +%s)" || return 1
  [[ "${now}" =~ ^[0-9]{10}$ ]] || return 1
  remaining=$((deadline - now - PRODUCTION_DEPLOY_CLOCK_SKEW_ALLOWANCE_SECONDS))
  if [[ -z "${required}" ]]; then
    required=$((PRODUCTION_DEPLOY_DRAIN_BUDGET_SECONDS + PRODUCTION_DEPLOY_CUTOVER_RESERVE_SECONDS))
  fi
  if ((remaining < required)); then
    echo "DEPLOY REFUSED: insufficient trusted job time remains for bounded drain and cutover/recovery reserve." >&2
    return 1
  fi
  printf '%s\n' "${remaining}"
}

# Freeze only the exact incumbent containers owned by the effective Compose
# release. Container IDs and immutable image identities are rechecked before
# every stop/start; an unknown app-image container or pre-existing warm
# candidate makes the cutover unprovable.
declare -a CUTOVER_IDS=() CUTOVER_SERVICES=() CUTOVER_STATES=() CUTOVER_NODES=() CUTOVER_INACTIVE_SERVICES=()
declare -A CUTOVER_IMAGE_IDS=() CUTOVER_IMAGE_REFS=() CUTOVER_REVISIONS=()
declare -A CUTOVER_RESTARTS=() CUTOVER_STOPPED_BY_US=()
CUTOVER_PHASE="idle"
CUTOVER_UNRESOLVED_STOP=0
CUTOVER_BINDING_JSON=""
capture_production_cutover() {
  local service ids id metadata container_image writer_spec
  local -a services=() probe_args=()
  services+=("${APP_SERVICES[@]}")
  if service_is_declared team-inbox-smtp; then
    services+=(team-inbox-smtp)
  fi
  CUTOVER_IDS=(); CUTOVER_SERVICES=(); CUTOVER_STATES=(); CUTOVER_NODES=(); CUTOVER_INACTIVE_SERVICES=()
  CUTOVER_IMAGE_IDS=(); CUTOVER_IMAGE_REFS=(); CUTOVER_REVISIONS=()
  CUTOVER_RESTARTS=(); CUTOVER_STOPPED_BY_US=()
  for service in "${services[@]}"; do
    ids="$("${COMPOSE[@]}" ps -a -q "${service}")" || return 1
    [[ -n "${ids}" ]] || {
      if [[ "${service}" == "team-inbox-smtp" ]]; then
        CUTOVER_INACTIVE_SERVICES+=("${service}")
        continue
      fi
      echo "CUTOVER REFUSED: expected Compose service ${service} has no captured container." >&2
      return 1
    }
    [[ "$(wc -l <<<"${ids}" | tr -d ' ')" == 1 ]] || {
      echo "CUTOVER REFUSED: service ${service} has an ambiguous container count." >&2
      return 1
    }
    id="${ids//$'\n'/}"
    metadata="$(docker inspect --format '{{.Id}}|{{.Config.Image}}|{{.Image}}|{{index .Config.Labels "org.opencontainers.image.revision"}}|{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.RestartCount}}|{{.Config.Hostname}}' "${id}")" || return 1
    IFS='|' read -r id container_image image_id revision state exit_code oom restart_count hostname <<<"${metadata}"
    [[ "${id}" =~ ^[0-9a-f]{64}$ && "${image_id}" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
    [[ "${container_image}" == "${PREV_IMAGE}" && "${revision}" == "${PREV_GIT_SHA}" ]] || {
      echo "CUTOVER REFUSED: incumbent ${service} image/revision differs from the pinned release." >&2
      return 1
    }
    if [[ "${service}" == team-inbox-smtp && "${state}" == exited && "${oom}" == false ]]; then
      : # Optional producer already stopped; preserve its original state.
    elif [[ "${state}" != running || "${oom}" != false ]]; then
      echo "CUTOVER REFUSED: incumbent ${service} is not running cleanly." >&2
      return 1
    fi
    if [[ "${service}" == celery-worker* ]]; then
      [[ "${hostname}" =~ ^[A-Za-z0-9_.@:-]{1,255}$ ]] || return 1
      CUTOVER_NODES+=("celery@${hostname}")
    fi
    CUTOVER_IDS+=("${id}"); CUTOVER_SERVICES+=("${service}"); CUTOVER_STATES+=("${state}")
    CUTOVER_IMAGE_IDS["${id}"]="${image_id}"
    CUTOVER_IMAGE_REFS["${id}"]="${container_image}"
    CUTOVER_REVISIONS["${id}"]="${revision}"
    CUTOVER_RESTARTS["${id}"]="${restart_count}"
    CUTOVER_STOPPED_BY_US["${id}"]=0
  done
  [[ "${#CUTOVER_NODES[@]}" -eq "${#CELERY_WORKER_SERVICES[@]}" ]] || {
    echo "CUTOVER REFUSED: could not capture all eight incumbent Celery worker nodes." >&2
    return 1
  }
  local -a expected_app_image_ids=()
  local i
  for i in "${!CUTOVER_IDS[@]}"; do
    [[ "${CUTOVER_IMAGE_REFS[${CUTOVER_IDS[$i]}]}" == "${PREV_IMAGE}" ]] \
      && expected_app_image_ids+=("${CUTOVER_IDS[$i]}")
  done
  [[ "$(docker ps -a --filter "ancestor=${IMAGE_REPO}" --format '{{.ID}}' | sort | tr '\n' ' ')" == "$(printf '%s\n' "${expected_app_image_ids[@]}" | sort | tr '\n' ' ')" ]] || {
    echo "CUTOVER REFUSED: an unknown or warm-candidate app-image container exists." >&2
    return 1
  }
  [[ -z "$(docker ps -a --filter "name=^/${CANDIDATE_CONTAINER}$" --format '{{.ID}}')" ]] || {
    echo "CUTOVER REFUSED: a warm candidate already exists." >&2
    return 1
  }
  probe_args=(capture --app-container "$(captured_id_for_service app)"
    --candidate-image "${IMAGE}" --candidate-revision "${FULL_SHA}")
  for i in "${!CUTOVER_IDS[@]}"; do
    service="${CUTOVER_SERVICES[$i]}"
    if [[ "${service}" == team-inbox-smtp && "${CUTOVER_STATES[$i]}" == exited ]]; then
      CUTOVER_INACTIVE_SERVICES+=("${service}")
      continue
    fi
    [[ "${service}" == app || "${CUTOVER_STATES[$i]}" != running ]] && continue
    probe_args+=(--writer "${service}=${CUTOVER_IDS[$i]}")
  done
  for service in "${CUTOVER_INACTIVE_SERVICES[@]}"; do
    probe_args+=(--inactive-service "${service}")
  done
  for id in "${COMPOSE[@]:2}"; do
    probe_args+=(--compose-argument "${id}")
  done
  if ! CUTOVER_BINDING_JSON="$(APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
      run_repo_module scripts.production_deploy_probe "${probe_args[@]}")"; then
    echo "CUTOVER REFUSED: exact database, broker, writer and candidate network bindings could not be captured." >&2
    CUTOVER_BINDING_JSON=""
    return 1
  fi
  [[ -n "${CUTOVER_BINDING_JSON}" ]] || return 1
}

assert_captured_container_unchanged() {
  local id="$1" metadata image_id image_ref revision state exit_code oom restarts hostname
  metadata="$(docker inspect --format '{{.Id}}|{{.Config.Image}}|{{.Image}}|{{index .Config.Labels "org.opencontainers.image.revision"}}|{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.RestartCount}}|{{.Config.Hostname}}' "${id}")" || return 1
  IFS='|' read -r id image_ref image_id revision state exit_code oom restarts hostname <<<"${metadata}"
  [[ "${image_id}" == "${CUTOVER_IMAGE_IDS[${id}]}" \
    && "${image_ref}" == "${CUTOVER_IMAGE_REFS[${id}]}" \
    && "${revision}" == "${CUTOVER_REVISIONS[${id}]}" \
    && "${restarts}" == "${CUTOVER_RESTARTS[${id}]}" \
    && "${oom}" == false ]]
}

stop_captured_container_gracefully() {
  local id="$1" timeout_seconds="$2" pid elapsed status
  assert_captured_container_unchanged "${id}" || { CUTOVER_UNRESOLVED_STOP=1; return 1; }
  [[ "$(docker inspect --format '{{.State.Status}}' "${id}")" == running ]] || { CUTOVER_UNRESOLVED_STOP=1; return 1; }
  docker stop --time 2147483647 "${id}" >/dev/null 2>&1 &
  pid=$!
  elapsed=0
  while ((elapsed < timeout_seconds)); do
    if ! kill -0 "${pid}" 2>/dev/null; then
      wait "${pid}" || { CUTOVER_UNRESOLVED_STOP=1; return 1; }
      status="$(docker inspect --format '{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.RestartCount}}' "${id}")" || { CUTOVER_UNRESOLVED_STOP=1; return 1; }
      [[ "${status}" == "exited|0|false|${CUTOVER_RESTARTS[${id}]}" ]] || { CUTOVER_UNRESOLVED_STOP=1; return 1; }
      CUTOVER_STOPPED_BY_US["${id}"]=1
      return 0
    fi
    sleep 1; elapsed=$((elapsed + 1))
  done
  # Stop waiting on the client only. The daemon receives no forced-kill
  # timeout; an unresolved container state blocks DDL and legacy restart.
  kill -TERM "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
  CUTOVER_UNRESOLVED_STOP=1
  echo "CUTOVER REFUSED: graceful stop of a captured service exceeded its bound; no DDL will run." >&2
  return 1
}

start_only_captured_containers() {
  local wanted_service id service
  for wanted_service in "${CELERY_WORKER_SERVICES[@]}" app bandwidth-poller syslog-listener team-inbox-smtp "${CELERY_BEAT_SERVICE}"; do
    for id in "${CUTOVER_IDS[@]}"; do
      service=""
      local i
      for i in "${!CUTOVER_IDS[@]}"; do
        if [[ "${CUTOVER_IDS[$i]}" == "${id}" ]]; then service="${CUTOVER_SERVICES[$i]}"; break; fi
      done
      [[ "${service}" == "${wanted_service}" && "${CUTOVER_STOPPED_BY_US[${id}]:-0}" == 1 ]] || continue
      assert_captured_container_unchanged "${id}" || return 1
      [[ "$(docker inspect --format '{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.RestartCount}}' "${id}")" == "exited|0|false|${CUTOVER_RESTARTS[${id}]}" ]] || return 1
      docker start "${id}" >/dev/null || return 1
      CUTOVER_STOPPED_BY_US["${id}"]=0
    done
  done
}

run_cutover_probe() {
  local image="$1" operation="$2" status
  shift 2
  [[ -n "${CUTOVER_BINDING_JSON}" ]] || return 2
  if APP_IMAGE="${IMAGE}" GIT_SHA="${FULL_SHA}" \
    run_repo_module scripts.production_deploy_probe run \
      --binding-json "${CUTOVER_BINDING_JSON}" --image "${image}" \
      --operation "${operation}" "$@"; then
    return 0
  else
    status=$?
    if [[ "${status}" -eq 3 ]]; then
      CUTOVER_UNRESOLVED_STOP=1
      echo "CUTOVER REFUSED: a Docker probe side effect has no proven terminal state; incumbent resume and DDL are blocked." >&2
    fi
    return "${status}"
  fi
}

verify_incumbent_predecessor_floors() {
  local app_id="${1:-}" captured_app
  captured_app="$(captured_id_for_service app)" || return 1
  [[ -z "${app_id}" || "${app_id}" == "${captured_app}" ]] || return 1
  # The reviewed reader is candidate-owned. It inspects the exact captured
  # incumbent database through the bound network; the incumbent image may
  # predate this reader entirely.
  run_cutover_probe candidate payment-email-predecessor >/dev/null \
    || { echo "CUTOVER REFUSED: incumbent Payment Email predecessor floor is unproved." >&2; return 1; }
  run_cutover_probe candidate payments-predecessor >/dev/null \
    || { echo "CUTOVER REFUSED: incumbent Payments predecessor floor is installed or unproved." >&2; return 1; }
}

verify_supported_payments_floor() {
  run_cutover_probe candidate payments-supported >/dev/null \
    || { echo "CUTOVER REFUSED: Payments schema is neither a complete supported predecessor nor installed floor." >&2; return 1; }
}

verify_installed_payments_floor() {
  local output expected_heads actual_heads
  output="$(run_cutover_probe candidate payments-installed 2>/dev/null)" || {
      echo "DEPLOY REFUSED: installed Payments floor could not be inspected after migrations." >&2
      return 1
    }
  if ! PAYMENT_FLOOR_INSPECTION="${output}" python3 - <<'PY'
import json
import os

try:
    value = json.loads(os.environ["PAYMENT_FLOOR_INSPECTION"])
    observation = value["observation"]
except (KeyError, TypeError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if value.get("schema_version") == 1
                 and value.get("floor") == "installed"
                 and isinstance(observation, dict)
                 and observation.get("legacy_shape_complete") is True
                 and observation.get("installed_shape_complete") is True else 1)
PY
  then
    echo "DEPLOY REFUSED: exact installed Payments catalog floor is not proven." >&2
    return 1
  fi
  expected_heads="$(candidate_heads | sort)" || return 1
  actual_heads="$(read_alembic_heads | sort)" || return 1
  [[ -n "${expected_heads}" && "${actual_heads}" == "${expected_heads}" ]] || {
    echo "DEPLOY REFUSED: post-migration heads do not exactly match the candidate's declared heads." >&2
    return 1
  }
}

captured_id_for_service() {
  local wanted="$1" i
  for i in "${!CUTOVER_SERVICES[@]}"; do
    if [[ "${CUTOVER_SERVICES[$i]}" == "${wanted}" ]]; then
      printf '%s\n' "${CUTOVER_IDS[$i]}"
      return 0
    fi
  done
  return 1
}
captured_state_for_service() {
  local wanted="$1" i
  for i in "${!CUTOVER_SERVICES[@]}"; do
    if [[ "${CUTOVER_SERVICES[$i]}" == "${wanted}" ]]; then
      printf '%s\n' "${CUTOVER_STATES[$i]}"
      return 0
    fi
  done
  return 1
}

assert_all_incumbent_writers_stopped() {
  local id service i state
  for i in "${!CUTOVER_IDS[@]}"; do
    id="${CUTOVER_IDS[$i]}"; service="${CUTOVER_SERVICES[$i]}"
    case " ${CELERY_WORKER_SERVICES[*]} ${CELERY_BEAT_SERVICE} ${PRODUCTION_PRODUCER_SERVICES[*]} " in
      *" ${service} "*) ;;
      *) continue ;;
    esac
    state="$(docker inspect --format '{{.State.Status}}' "${id}")" || return 1
    [[ "${state}" == exited ]] || {
      echo "CUTOVER REFUSED: captured incumbent writer ${service} is still running." >&2
      return 1
    }
  done
}

prepare_production_writer_cutover() {
  local remaining app_id service id
  [[ "${DEPLOYMENT_TARGET}" == production \
    && "${BACKUP_MODE}" != skip_production_post_migration_resume ]] || return 0
  if ! remaining="$(require_production_cutover_budget)"; then return 1; fi
  if [[ "${DOTMAC_MAINTENANCE_CONTEXT_FD:-}" == 7 ]]; then
    [[ "${BACKUP_MODE}" == required ]] || {
      echo "CUTOVER REFUSED: maintenance repair requires a fresh verified production backup." >&2
      return 1
    }
    local maintenance_context parsed binding_encoded heads_encoded
    if ! maintenance_context="$(run_repo_module scripts.production_deploy_migration \
      maintenance-context --fd 7)"; then
      echo "CUTOVER REFUSED: inherited maintenance context or retained stopped-state proof is invalid." >&2
      return 1
    fi
    if ! parsed="$(input_payload="${maintenance_context}" python3 -c 'import base64,json,os; p=json.loads(os.environ["input_payload"]); assert p.get("schema_version")==1; b=p["binding"]; h=p["heads_before"]; assert isinstance(b,dict) and isinstance(h,list) and h; print(base64.b64encode(json.dumps(b,separators=(",",":")).encode()).decode()); print(base64.b64encode(json.dumps(h,separators=(",",":")).encode()).decode())')"; then
      echo "CUTOVER REFUSED: maintenance proof response is malformed." >&2
      return 1
    fi
    binding_encoded="${parsed%%$'\n'*}"
    heads_encoded="${parsed#*$'\n'}"
    CUTOVER_BINDING_JSON="$(base64 -d <<<"${binding_encoded}")" || return 1
    MIGRATION_HEADS_BEFORE="$(base64 -d <<<"${heads_encoded}" | python3 -c 'import json,sys; h=json.load(sys.stdin); assert isinstance(h,list) and h; print("\n".join(h))')" || return 1
    [[ -n "${CUTOVER_BINDING_JSON}" && -n "${MIGRATION_HEADS_BEFORE}" ]] || return 1
    CUTOVER_PHASE="maintenance-stopped-before-ddl"
    log "Verified retained installed-floor migration state and exact stopped incumbent; running forward repair"
    return 0
  fi
  log "Capturing exact incumbent service identities before writer drain"
  capture_production_cutover || return 1
  app_id="$(captured_id_for_service app)" || return 1
  verify_supported_payments_floor || return 1
  MIGRATION_HEADS_BEFORE="$(read_alembic_heads)" || {
    echo "CUTOVER REFUSED: starting Alembic heads are not readable." >&2
    return 1
  }
  CUTOVER_PHASE="preflight"

  # Beat can create new scheduled work; then stop every incumbent producer.
  id="$(captured_id_for_service "${CELERY_BEAT_SERVICE}" 2>/dev/null || true)"
  if [[ -n "${id}" ]]; then stop_captured_container_gracefully "${id}" 60 || return 1; fi
  for service in app bandwidth-poller syslog-listener team-inbox-smtp; do
    id="$(captured_id_for_service "${service}" 2>/dev/null || true)"
    if [[ -n "${id}" && "${CUTOVER_STOPPED_BY_US[${id}]}" == 0 \
      && "$(captured_state_for_service "${service}")" == running ]]; then
      stop_captured_container_gracefully "${id}" 120 || return 1
    fi
  done
  CUTOVER_PHASE="draining-workers"
  if ! observe_writer_drain wait-for-stable-empty workers-and-broker; then return 1; fi

  for service in "${CELERY_WORKER_SERVICES[@]}"; do
    id="$(captured_id_for_service "${service}")" || return 1
    stop_captured_container_gracefully "${id}" 60 || return 1
  done
  CUTOVER_PHASE="final-broker-proof"
  observe_writer_drain wait-for-stable-empty broker-only || return 1
  assert_all_incumbent_writers_stopped || return 1
  local heads_now
  heads_now="$(read_alembic_heads)" || return 1
  [[ "${heads_now}" == "${MIGRATION_HEADS_BEFORE}" ]] || {
    echo "CUTOVER REFUSED: Alembic heads changed during the writer drain." >&2
    return 1
  }
  if ! require_production_cutover_budget "${PRODUCTION_DEPLOY_CUTOVER_RESERVE_SECONDS}" >/dev/null; then
    echo "CUTOVER REFUSED: remaining job time cannot cover migrations, startup health and recovery; the incumbent is being resumed." >&2
    return 1
  fi
  verify_supported_payments_floor || return 1
  run_cutover_probe candidate verify >/dev/null || {
    echo "CUTOVER REFUSED: captured database, broker, network or named-app identity changed before DDL." >&2
    return 1
  }
  CUTOVER_PHASE="stopped-before-ddl"
  log "Exact incumbent writers are stopped and two broker-empty proofs are complete"
}

resume_exact_incumbent_preddl() {
  local heads_now app_id
  [[ "${CUTOVER_PHASE:-idle}" != idle && "${CUTOVER_PHASE}" != resumed-preddl \
    && "${CUTOVER_PHASE}" != installed-floor-proven ]] || return 1
  [[ "${CUTOVER_UNRESOLVED_STOP}" == 0 ]] || {
    echo "CUTOVER RECOVERY REFUSED: a graceful service stop remains unresolved." >&2
    return 1
  }
  heads_now="$(read_alembic_heads)" || return 1
  [[ "${heads_now}" == "${MIGRATION_HEADS_BEFORE}" ]] || return 1
  app_id="$(captured_id_for_service app)" || return 1
  verify_incumbent_predecessor_floors "${app_id}" || return 1
  local i id status
  for i in "${!CUTOVER_IDS[@]}"; do
    id="${CUTOVER_IDS[$i]}"
    status="$(docker inspect --format '{{.State.Status}}' "${id}")" || return 1
    if [[ "${CUTOVER_STOPPED_BY_US[${id}]}" == 0 ]]; then
      [[ "${status}" == running ]] || return 1
      assert_captured_container_unchanged "${id}" || return 1
    else
      [[ "${status}" == exited ]] || return 1
    fi
  done
  start_only_captured_containers || return 1
  wait_for_health "${HEALTH_URL}" "Resumed exact incumbent" "" \
    "${HEALTH_TIMEOUT_SECONDS}" "${READY_URL}" || return 1
  wait_for_background_runtime || return 1
  CUTOVER_PHASE="resumed-preddl"
}

validate_writer_observation() {
  local mode="$1" expected_outcome="$2" json="$3" nodes_csv="$4"
  WRITER_OBSERVATION_JSON="${json}" python3 - "${mode}" "${expected_outcome}" "${nodes_csv}" <<'PY'
import json
import os
import sys

mode, expected_outcome, nodes_csv = sys.argv[1:]
expected_nodes = set(nodes_csv.split(",")) if nodes_csv else set()
try:
    payload = json.loads(os.environ["WRITER_OBSERVATION_JSON"])
except Exception:
    raise SystemExit(1)

def zero_observation(value):
    if not isinstance(value, dict) or value.get("mode") != mode:
        return False
    workers = value.get("workers")
    queues = value.get("queues")
    unacked = value.get("unacknowledged")
    if not isinstance(workers, list) or not isinstance(queues, list):
        return False
    if mode == "workers-and-broker":
        actual = {item.get("node") for item in workers if isinstance(item, dict)}
        if actual != expected_nodes or len(workers) != len(expected_nodes):
            return False
        if any(any(type(item.get(field)) is not int or item[field] != 0
                   for field in ("active", "reserved", "scheduled"))
               for item in workers):
            return False
    elif workers:
        return False
    if len(queues) != 11 or len({item.get("queue") for item in queues if isinstance(item, dict)}) != 11:
        return False
    if any(not isinstance(item, dict) or type(item.get("ready")) is not int or item["ready"] != 0
           for item in queues):
        return False
    return (isinstance(unacked, dict)
            and type(unacked.get("hash_entries")) is int and unacked["hash_entries"] == 0
            and type(unacked.get("index_entries")) is int and unacked["index_entries"] == 0)

if payload.get("schema_version") != 1 or payload.get("mode") != mode:
    raise SystemExit(1)
if payload.get("outcome") != expected_outcome or payload.get("refusal") is not None:
    raise SystemExit(1)
if not zero_observation(payload.get("observation")):
    raise SystemExit(1)
if expected_outcome == "stable-empty" and not zero_observation(payload.get("previous_zero")):
    raise SystemExit(1)
PY
}

observe_writer_drain() {
  local operation="$1" mode="$2" expected="${3:-}" output rc
  local -a args=(--mode "${mode}" --drain-operation "${operation}" \
    --drain-budget "${PRODUCTION_DEPLOY_DRAIN_BUDGET_SECONDS}")
  if [[ "${mode}" == workers-and-broker ]]; then
    local node
    for node in "${CUTOVER_NODES[@]}"; do args+=(--expected-node "${node}"); done
    expected="$(IFS=,; echo "${CUTOVER_NODES[*]}")"
  fi
  if output="$(run_cutover_probe candidate drain "${args[@]}" 2>/dev/null)"; then
    rc=0
  else
    rc=$?
  fi
  [[ "${rc}" -eq 0 ]] || {
    echo "CUTOVER REFUSED: writer drain observation was busy, incomplete or refused (exit ${rc})." >&2
    return 1
  }
  if [[ "${operation}" == wait-for-stable-empty ]]; then
    validate_writer_observation "${mode}" stable-empty "${output}" "${expected}" || {
      echo "CUTOVER REFUSED: writer drain response did not prove the exact zero-state contract." >&2
      return 1
    }
  else
    validate_writer_observation "${mode}" observed-empty "${output}" "${expected}" || return 1
  fi
}
# Shared by a failed migration and an INT/TERM/HUP during it. The pin is put
# back ONLY when the typed journal proves exact terminal evidence, unchanged
# heads, and both predecessor floors; never via restore_prev. Always returns
# 1 (the deploy has failed). A missing acknowledgment or unknown state blocks
# resume.
migration_container_gone() {
  [[ "${CUTOVER_PHASE:-idle}" != idle ]] || return 0
  [[ "${MIGRATION_INVOCATION_STARTED}" == 1 ]] || return 0
  run_repo_module scripts.production_deploy_migration \
    require-safe-preddl --deploy-directory "${DEPLOY_DIR}" >/dev/null 2>&1
}
settle_failed_migration() {
  local heads_after running_image
  if [[ "${DOTMAC_MAINTENANCE_CONTEXT_FD:-}" == 7 ]]; then
    echo "Maintenance migration did not complete; the original stopped incumbent and candidate pin are retained for forward recovery." >&2
    return 1
  fi
  if migration_container_gone; then
    heads_after="$(read_alembic_heads)" || heads_after=""
  else
    echo "Migration container still running or its state is unknown; not trusting a heads read. It was not stopped." >&2
    heads_after=""
  fi
  if [[ -z "${MIGRATION_HEADS_BEFORE}" || -z "${heads_after}" || "${MIGRATION_HEADS_BEFORE}" != "${heads_after}" ]]; then
    echo "Alembic heads before: ${MIGRATION_HEADS_BEFORE:-<unreadable>}" >&2
    echo "Alembic heads after:  ${heads_after:-<unreadable>}" >&2
    echo "Schema may have advanced (or could not be proven unchanged); .env pin left at ${IMAGE}. Do not recreate containers; repair forward." >&2
    return 1
  fi
  if [[ "${CUTOVER_PHASE:-idle}" != idle ]]; then
    app_id="$(captured_id_for_service app)" || app_id=""
    if [[ -z "${app_id}" ]] || ! verify_incumbent_predecessor_floors "${app_id}"; then
      echo "Migration heads are unchanged, but the incumbent predecessor floor is not proven; services remain stopped for forward repair." >&2
      return 1
    fi
  fi
  running_image="$(docker inspect "${APP_CONTAINER}" --format '{{.Config.Image}}' 2>/dev/null || true)"
  if repin_prev; then
    if [[ "${CUTOVER_PHASE:-idle}" != idle ]]; then
      if ! resume_exact_incumbent_preddl; then
        echo "Migration heads are unchanged, but exact incumbent resume failed; candidate pin was restored and services require forward/operator repair." >&2
        return 1
      fi
      echo "Migration heads measured unchanged (${heads_after//$'\n'/ }); exact captured incumbent containers resumed and healthy." >&2
      return 1
    fi
    if [[ -z "${PREV_IMAGE}" && -z "${running_image}" ]]; then
      echo "Restored .env pin to <unset> (GIT_SHA ${PREV_GIT_SHA:-<unset>}): no previous pin and no running ${APP_CONTAINER} container (first deploy)." >&2
    elif [[ "${running_image}" == "${PREV_IMAGE}" ]]; then
      echo "Restored .env pin to ${PREV_IMAGE:-<unset>} (GIT_SHA ${PREV_GIT_SHA:-<unset>}), which matches the running ${APP_CONTAINER} image." >&2
    else
      echo "WARNING: restored .env pin to ${PREV_IMAGE:-<unset>}, but ${APP_CONTAINER} reports image '${running_image:-unknown}'; verify before recreating containers." >&2
    fi
    echo "Alembic heads measured unchanged (${heads_after//$'\n'/ }). No container was restarted or recreated and no rollback path was used. Repair forward and redeploy." >&2
  else
    echo "DEPLOY PIN RESTORE FAILED: .env may still pin ${IMAGE}; set APP_IMAGE=${PREV_IMAGE:-<unset>} GIT_SHA=${PREV_GIT_SHA:-<unset>} by hand before any container is recreated." >&2
  fi
  return 1
}
MIGRATION_HEADS_BEFORE=""
if [[ "${DEPLOYMENT_TARGET}" == production \
  && "${BACKUP_MODE}" != skip_production_post_migration_resume ]]; then
  if ! prepare_production_writer_cutover; then
    if [[ "${CUTOVER_PHASE:-idle}" != idle ]]; then
      if ! resume_exact_incumbent_preddl; then
        echo "CUTOVER RECOVERY REFUSED: incumbent could not be proven safe to resume; no migration was started." >&2
      fi
    fi
    trap - ERR
    exit 1
  fi
fi
pin_and_migrate() {
  if [[ "${CUTOVER_PHASE:-idle}" == idle ]]; then MIGRATION_HEADS_BEFORE=""; fi
  if [[ "${BACKUP_MODE}" != "skip_production_post_migration_resume" \
    && "${CUTOVER_PHASE:-idle}" == idle ]]; then
    MIGRATION_HEADS_BEFORE="$(read_alembic_heads)" || MIGRATION_HEADS_BEFORE=""
  fi

  # Interrupts during the pin/migration window use the same measured-heads
  # decision as a failure; the guarded restore_prev (which the payment-email
  # floor would refuse, leaving drift) is not used. Scoped to this function.
  trap 'trap - ERR; cleanup_children; settle_failed_migration || true; echo "Deploy interrupted during migration; see pin state above" >&2; exit 130' INT TERM HUP

  log "Pinning APP_IMAGE=${IMAGE} and GIT_SHA=${FULL_SHA}"
  if ! set_env_value APP_IMAGE "${IMAGE}" || ! set_env_value GIT_SHA "${FULL_SHA}"; then
    if [[ "${DOTMAC_MAINTENANCE_CONTEXT_FD:-}" == 7 ]]; then
      echo "DEPLOY PIN WRITE FAILED during maintenance repair; keep all captured containers stopped and resolve the pin manually before retrying." >&2
      install_deploy_interrupt_trap
      return 1
    fi
    echo "DEPLOY PIN WRITE FAILED: restoring the previous pin; nothing was migrated or recreated." >&2
    if ! repin_prev; then
      echo "DEPLOY PIN RESTORE FAILED: .env may be half-updated; set APP_IMAGE=${PREV_IMAGE:-<unset>} GIT_SHA=${PREV_GIT_SHA:-<unset>} by hand before any container is recreated." >&2
    fi
    if [[ "${CUTOVER_PHASE:-idle}" != idle ]]; then
      resume_exact_incumbent_preddl || echo "DEPLOY RECOVERY REFUSED: exact incumbent could not be resumed after pin-write failure." >&2
    fi
    install_deploy_interrupt_trap
    return 1
  fi

  # Multi-head safe: sub has hit multi-head states (e.g. the bundles migration that
  # merged heads), so use `heads` (plural), never `head`.
  log "Applying migrations (alembic upgrade heads)"
  if [[ "${BACKUP_MODE}" == "skip_production_post_migration_resume" ]]; then
    log "Skipping migrations under verified post-migration resume evidence"
    install_deploy_interrupt_trap
    return 0
  fi
  if run_migrations; then
    if [[ "${CUTOVER_PHASE:-idle}" != idle ]]; then
      CUTOVER_PHASE="ddl-applied"
      if ! verify_installed_payments_floor; then
        install_deploy_interrupt_trap
        return 1
      fi
      CUTOVER_PHASE="installed-floor-proven"
    fi
    install_deploy_interrupt_trap
    return 0
  fi
  echo "DEPLOY MIGRATION FAILED before any new container served traffic." >&2
  # Settle BEFORE restoring the original trap: an INT during the settle must
  # still take the measured-heads path, not restore_prev.
  settle_failed_migration || true
  install_deploy_interrupt_trap
  return 1
}
# --- End pre-swap pin and migration -------------------------------------------
if ! pin_and_migrate; then
  trap - ERR
  exit 1
fi

log "Verifying database schema contracts"
"${COMPOSE[@]}" run --rm --no-deps app \
  python -m scripts.migration.verify_schema_contracts

log "Verifying enabled integration manifest pins"
"${COMPOSE[@]}" run --rm --no-deps app \
  python -m scripts.integrations.verify_manifest_pins

log "Rechecking installed feature secret requirements before service replacement"
preflight_feature_secret_requirements after_migrations

log "Starting warm candidate on 127.0.0.1:${CANDIDATE_PORT} (health timeout ${CANDIDATE_HEALTH_TIMEOUT_SECONDS}s)"
docker rm -f "${CANDIDATE_CONTAINER}" >/dev/null 2>&1 || true
# Do not use `--rm`: an early process exit must leave its state and bounded log
# stream available to `report_candidate_failure` before rollback cleanup.
"${COMPOSE[@]}" run --no-deps -d \
  --name "${CANDIDATE_CONTAINER}" \
  -p "127.0.0.1:${CANDIDATE_PORT}:8001" \
  app >/dev/null
CANDIDATE_STARTED=1
assert_no_source_mount "${CANDIDATE_CONTAINER}"
require_candidate_health

log "Starting candidate workers before any producers"
PRIMARY_REPLACED=1
"${COMPOSE[@]}" up -d --force-recreate "${CELERY_WORKER_SERVICES[@]}"
if [[ "${CUTOVER_PHASE:-idle}" == installed-floor-proven ]]; then
  wait_for_background_workers
fi

declare -a PRODUCER_SERVICES_TO_START=()
for service in "${APP_SERVICES[@]}"; do
  case " ${CELERY_WORKER_SERVICES[*]} ${CELERY_BEAT_SERVICE} " in
    *" ${service} "*) continue ;;
  esac
  PRODUCER_SERVICES_TO_START+=("${service}")
done
log "Starting application and producers after worker readiness"
if ((${#PRODUCER_SERVICES_TO_START[@]})); then
  "${COMPOSE[@]}" up -d --force-recreate "${PRODUCER_SERVICES_TO_START[@]}"
fi
if service_is_declared team-inbox-smtp; then
  "${COMPOSE[@]}" up -d --force-recreate team-inbox-smtp
fi

log "Verifying deploy integrity (no host source bind-mount shadowing the image)"
if ! assert_no_source_mount; then
  trap - ERR
  exit 1
fi

# Nginx serves the healthy candidate while Compose replaces the primary.
log "Waiting for app health at ${HEALTH_URL} and readiness at ${READY_URL} (timeout ${HEALTH_TIMEOUT_SECONDS}s)"
if ! wait_for_health "${HEALTH_URL}" "Primary app" "" \
  "${HEALTH_TIMEOUT_SECONDS}" "${READY_URL}"; then
  trap - ERR
  log "Health gate FAILED (${HEALTH_URL} never became healthy) — checking previous-image rollback floor"
  if [[ -n "${PREV_IMAGE}" ]]; then
    if restore_prev; then
      log "Rolled back to ${PREV_IMAGE}. NOTE: migrations from ${TAG} were NOT reverted."
    else
      report_rollback_outcome
    fi
  else
    log "No previous image recorded — candidate retained where available. Repair forward."
  fi
  exit 1
fi

if service_is_declared "${CELERY_BEAT_SERVICE}"; then
  log "Starting Beat after workers and application producers are healthy"
  "${COMPOSE[@]}" up -d --force-recreate "${CELERY_BEAT_SERVICE}"
fi

log "Verifying Celery workers and Beat (timeout ${BACKGROUND_RUNTIME_TIMEOUT_SECONDS}s)"
if ! wait_for_background_runtime; then
  trap - ERR
  log "Background runtime health gate FAILED — checking previous-image rollback floor"
  if [[ -n "${PREV_IMAGE}" ]]; then
    if restore_prev; then
      log "Rolled back to ${PREV_IMAGE}. NOTE: migrations from ${TAG} were NOT reverted."
    else
      report_rollback_outcome
    fi
  else
    log "No previous image recorded — healthy candidate retained. Investigate the Celery containers."
  fi
  exit 1
fi

log "Primary is healthy; draining warm candidate"
stop_candidate_gracefully

trap - ERR
log "Deployed ${TAG} successfully (was ${PREV_IMAGE:-none})"
# Part of the deployment receipt: a promotion should be able to show which
# of the three prerequisite outcomes this release actually took.
log "DEPLOY RECEIPT: tag=${TAG} revision=${FULL_SHA} prerequisites=${PREREQUISITE_OUTCOME}"

# After the application is accepted, so a config problem in these services can
# never roll back a healthy application release.
log "Applying changed service configuration (vmagent, promtail, freeradius)"
apply_service_config_changes

log "Pruning old ${IMAGE_REPO} images (keeping ${IMAGE_RETAIN_COUNT} rollback images)"
if ! IMAGE_REPO="${IMAGE_REPO}" RETAIN_IMAGES="${IMAGE_RETAIN_COUNT}" \
  bash "${REPO_DIR}/scripts/docker_image_retention.sh"; then
  log "Image retention failed; deploy is healthy, but old image cleanup needs attention"
  exit 1
fi
log "Image retention completed and verified"

if [[ "${CONFIG_APPLY_FAILED}" == "1" ]]; then
  log "Application release is healthy, but a FreeRADIUS configuration change was NOT applied (see above)."
  exit 1
fi
