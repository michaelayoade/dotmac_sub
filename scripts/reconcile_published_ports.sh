#!/usr/bin/env bash
# Reconcile ONE infrastructure service's published ports with the declared
# intent in deploy/published_ports.toml.
#
# WHY THIS EXISTS
#
# deploy.sh's APP_SERVICES deliberately excludes postgres-local, redis-local,
# nominatim, freeradius, genieacs and the metrics services. A deploy never
# recreates them, so editing a published port in docker-compose.yml does not
# change a running host -- the container keeps whatever binding it was created
# with. Before this script there was no managed way to apply such a change at
# all. The alternatives were a hand-edit on the box (unattributable, and
# production's checkout is already divergent from origin/main) or nothing.
#
# It is deliberately NOT part of a deploy. Recreating the database on every
# release would be far worse than any binding bug. This is a named, requested,
# recorded maintenance action, and it reconciles exactly one service.
#
# THE ORDERING THIS SCRIPT EXISTS TO ENFORCE
#
# The environment value must be in place BEFORE the recreate. Recreating
# postgres-local while PG_LOCAL_BIND is unset applies compose's loopback
# default and binds the replication standby out of its own WAL stream --
# turning a security fix into a replication outage.
#
# That ordering is not a runbook sentence here. It is structural:
#
#   gate 1  `plan` refuses a bind that does not admit a declared required
#           client, so a narrowing bind never reaches a host at all.
#   gate 2  the env value is written, then RE-READ out of .env and compared.
#   gate 3  `docker compose config` is asked what it NOW actually resolves,
#           and the recreate is refused if that is not the planned bind.
#   gate 4  only gate 3 sets VERIFIED_SERVICE, and the recreate names it.
#           Under `set -u`, deleting or skipping gate 3 makes the recreate
#           abort rather than run unverified. The recreate then proves the
#           container id actually changed.
#   gate 5  the ACTUAL listeners are re-read and must match the declaration
#           in BOTH address families.
#
# Usage:
#   reconcile_published_ports.sh --service <name> --environment <env>
#   reconcile_published_ports.sh --service <name> --environment <env> --plan-only
#   reconcile_published_ports.sh --list
#
#   --plan-only   run gates 1-3 and print the plan; change no container. Any
#                 .env value written to reach gate 3 is rolled back.
#   --list        show declared services and environments, then exit.
#
# Env:
#   DEPLOY_DIR    directory holding .env and the compose project
#   REPO_DIR      checkout supplying the declaration and helper modules
#   RECONCILE_LOCK_FILE
#                 defaults to the DEPLOY lock, so a reconcile and a deploy can
#                 never run at once.
#   IGNORE_COMPOSE_OVERRIDE=1
#                 ignore docker-compose.override.yml even if present.
#
# Exit codes:
#   0  reconciled -- the service was recreated and now matches the declaration
#   1  refused    -- a gate failed; see stderr
#   2  usage
#   3  ALREADY RECONCILED -- nothing needed doing. Deliberately distinct from
#      0, so "the operation did nothing" can never be read as "the operation
#      fixed it".

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DEPLOY_DIR="${DEPLOY_DIR:-${REPO_DIR}}"
ENV_FILE="${DEPLOY_DIR}/.env"
RELEASE_COMPOSE_FILE="${REPO_DIR}/docker-compose.yml"
HOST_COMPOSE_OVERRIDE="${DEPLOY_DIR}/docker-compose.override.yml"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RECREATE_TIMEOUT_SECONDS="${RECREATE_TIMEOUT_SECONDS:-120}"

log() { printf '\n==> %s\n' "$*"; }
die() {
  echo "PORT RECONCILE REFUSED: $*" >&2
  exit 1
}
usage() {
  echo "usage: reconcile_published_ports.sh --service <name> --environment <env>" >&2
  echo "       [--plan-only]" >&2
  echo "       reconcile_published_ports.sh --list" >&2
  exit 2
}

run_repo_module() {
  REPO_DIR="${REPO_DIR}" PYTHON_BIN="${PYTHON_BIN}" \
    bash "${REPO_DIR}/scripts/run_repo_module.sh" "$@"
}

# Same shape as scripts/deploy.sh's reader: never source a deploy .env, that
# pulls every secret into shell state.
env_value() {
  local key="$1"
  grep -E "^${key}=" "${ENV_FILE}" 2>/dev/null | tail -n 1 | cut -d= -f2- || true
}

env_has_key() {
  grep -qE "^$1=" "${ENV_FILE}" 2>/dev/null
}

# Same in-place edit shape as scripts/deploy.sh's set_env_value: the suffixed
# `sed -i` is the GNU/BSD-portable form, and the $$ keeps concurrent runs from
# clobbering each other's temp file.
set_env_value() {
  local key="$1" value="$2" suffix=".reconcile-$$.bak"
  if env_has_key "${key}"; then
    sed -i"${suffix}" "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >>"${ENV_FILE}"
  fi
  rm -f -- "${ENV_FILE}${suffix}"
}

unset_env_value() {
  local key="$1" suffix=".reconcile-$$.bak"
  sed -i"${suffix}" "/^${key}=/d" "${ENV_FILE}"
  rm -f -- "${ENV_FILE}${suffix}"
}

RESTORE_KEYS=()
RESTORE_PRESENT=()
RESTORE_VALUES=()

restore_env() {
  local index
  for index in "${!RESTORE_KEYS[@]}"; do
    if [[ "${RESTORE_PRESENT[${index}]}" == "1" ]]; then
      set_env_value "${RESTORE_KEYS[${index}]}" "${RESTORE_VALUES[${index}]}"
    else
      unset_env_value "${RESTORE_KEYS[${index}]}"
    fi
  done
}

# --- arguments -------------------------------------------------------------

SERVICE=""
ENVIRONMENT=""
PLAN_ONLY=0
LIST_ONLY=0

(($#)) || usage
while (($#)); do
  case "$1" in
    --service)
      (($# >= 2)) || usage
      SERVICE="$2"
      shift 2
      ;;
    --environment)
      (($# >= 2)) || usage
      ENVIRONMENT="$2"
      shift 2
      ;;
    --plan-only)
      PLAN_ONLY=1
      shift
      ;;
    --list)
      LIST_ONLY=1
      shift
      ;;
    *) usage ;;
  esac
done

if ((LIST_ONLY == 1)); then
  run_repo_module scripts.published_ports list
  exit 0
fi

[[ -n "${SERVICE}" ]] || usage
[[ -n "${ENVIRONMENT}" ]] || usage
[[ -f "${ENV_FILE}" ]] || die "missing ${ENV_FILE}"
command -v docker >/dev/null || die "docker is not on PATH"

COMPOSE=(docker compose --project-directory "${DEPLOY_DIR}"
  --env-file "${ENV_FILE}" -f "${RELEASE_COMPOSE_FILE}")
if [[ -f "${HOST_COMPOSE_OVERRIDE}" && "${IGNORE_COMPOSE_OVERRIDE:-0}" != "1" ]]; then
  COMPOSE+=(-f "${HOST_COMPOSE_OVERRIDE}")
fi

# --- gate 0: the host must BE the environment it is reconciled as ----------
#
# Without this, `--environment production` run on staging would write
# production's binds into staging's .env and recreate against them.
HOST_ENVIRONMENT=""
case "$(env_value APP_ENV):$(env_value SERVER_NAME)" in
  production:dotmac-sub-prod) HOST_ENVIRONMENT="production" ;;
  staging:dotmac-sub-staging) HOST_ENVIRONMENT="staging" ;;
  *) die "APP_ENV/SERVER_NAME in ${ENV_FILE} do not identify an approved host." ;;
esac
[[ "${HOST_ENVIRONMENT}" == "${ENVIRONMENT}" ]] ||
  die "this host is '${HOST_ENVIRONMENT}' but --environment says '${ENVIRONMENT}'."

# --- one at a time, sharing the deploy lock --------------------------------
LOCK_FILE="${RECONCILE_LOCK_FILE:-${DEPLOY_LOCK_FILE:-/var/lock/dotmac_sub_deploy.lock}}"
command -v flock >/dev/null ||
  die "flock(1) not found; cannot guarantee a deploy is not running concurrently."
if ! { exec 9>"${LOCK_FILE}"; } 2>/dev/null; then
  die "cannot open reconcile lock ${LOCK_FILE}"
fi
if ! flock -n 9; then
  die "another deploy or reconcile already holds ${LOCK_FILE}."
fi

# --- gate 1: plan, which refuses a bind that strands a required client -----
log "Planning ${SERVICE} for ${ENVIRONMENT}"
if ! PLAN="$(run_repo_module scripts.published_ports plan \
  --service "${SERVICE}" --environment "${ENVIRONMENT}")"; then
  die "no admissible plan for ${SERVICE} in ${ENVIRONMENT} (see above)."
fi
echo "${PLAN}"

plan_query() {
  printf '%s' "${PLAN}" | "${PYTHON_BIN}" -c "$@"
}

ASSIGNMENT_KEYS="$(plan_query 'import json,sys
print(" ".join(sorted(json.load(sys.stdin)["assignments"])))')"
[[ -n "${ASSIGNMENT_KEYS}" ]] || die "the plan produced no environment assignments."

# --- observe the state BEFORE anything changes -----------------------------
collect_listeners() {
  local out="$1" ids
  ids="$("${COMPOSE[@]}" ps -q 2>/dev/null || true)"
  [[ -n "${ids}" ]] || die "the compose project has no running containers."
  # Exactly three fields leave the container. The env block never does.
  # shellcheck disable=SC2086
  docker inspect ${ids} --format \
    '{"service":{{json (index .Config.Labels "com.docker.compose.service")}},"container":{{json .Name}},"ports":{{json .NetworkSettings.Ports}}}' |
    "${PYTHON_BIN}" -c 'import json, sys
rows = [json.loads(line) for line in sys.stdin if line.strip()]
for row in rows:
    row["container"] = row["container"].lstrip("/")
with open(sys.argv[1], "w") as handle:
    json.dump(rows, handle)' "${out}"
}

BEFORE_JSON="$(mktemp)"
AFTER_JSON="$(mktemp)"

# Set to 1 once .env has been written and while that write is still meant to be
# undone; cleared at the point of no return, just before the recreate. A
# killed run therefore never leaves a half-applied .env behind.
CLEANUP_RESTORE_ENV=0
cleanup() {
  rm -f -- "${BEFORE_JSON}" "${AFTER_JSON}"
  if ((CLEANUP_RESTORE_ENV == 1)); then
    restore_env
    echo "Interrupted before the recreate: ${ENV_FILE} restored." >&2
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

collect_listeners "${BEFORE_JSON}"
LISTENERS_ALREADY_MATCH=0
if run_repo_module scripts.published_ports check-listeners \
  --environment "${ENVIRONMENT}" --service "${SERVICE}" \
  --observed "${BEFORE_JSON}" >/dev/null 2>&1; then
  LISTENERS_ALREADY_MATCH=1
fi

# --- gate 2: put the environment values in place, then PROVE they took -----
ENV_CHANGED=0
for key in ${ASSIGNMENT_KEYS}; do
  want="$(plan_query 'import json,sys
print(json.load(sys.stdin)["assignments"][sys.argv[1]])' "${key}")"
  have="$(env_value "${key}")"
  present=0
  if env_has_key "${key}"; then
    present=1
  fi
  RESTORE_KEYS+=("${key}")
  RESTORE_PRESENT+=("${present}")
  RESTORE_VALUES+=("${have}")
  if ((present == 1)) && [[ "${have}" == "${want}" ]]; then
    log "${key} is already ${want}"
    continue
  fi
  log "Setting ${key}=${want} in ${ENV_FILE}"
  set_env_value "${key}" "${want}"
  ENV_CHANGED=1
  CLEANUP_RESTORE_ENV=1
  # Re-read rather than trusting the write.
  [[ "$(env_value "${key}")" == "${want}" ]] ||
    die "${key} did not take the planned value in ${ENV_FILE}."
done

# --- gate 3: ask compose what it NOW resolves, before touching a container -
#
# This is the step that makes "env before recreate" a checked precondition
# rather than a documented one. It is also the ONLY step that sets
# VERIFIED_SERVICE, which gate 4 names -- so an edit that drops this gate makes
# the recreate abort under `set -u` instead of running unverified.
log "Verifying the effective compose configuration"
if ! EFFECTIVE="$("${COMPOSE[@]}" config --format json)"; then
  restore_env
  die "docker compose config failed; ${ENV_FILE} restored."
fi

if ! printf '%s' "${EFFECTIVE}" | "${PYTHON_BIN}" -c '
import json, sys

plan = json.loads(sys.argv[1])
service = plan["service"]
config = json.load(sys.stdin)
definition = (config.get("services") or {}).get(service)
if definition is None:
    sys.exit(f"compose resolves no service named {service!r}")

actual: dict[str, set[str]] = {}
for entry in definition.get("ports") or []:
    key = f"{service}:{int(entry['published'])}/{entry.get('protocol') or 'tcp'}"
    actual.setdefault(key, set()).add(entry.get("host_ip") or "")

planned = {target["key"]: target for target in plan["targets"]}
problems = []
for key, target in sorted(planned.items()):
    want = target["bind"].rstrip(":")
    got = actual.get(key)
    if got is None:
        problems.append(f"{key}: compose resolves no such publish")
    elif got != {want}:
        problems.append(f"{key}: compose resolves host_ip {sorted(got)}, planned [{want!r}]")
for key in sorted(set(actual) - set(planned)):
    problems.append(f"{key}: compose publishes it but the plan does not cover it")
if problems:
    sys.exit("; ".join(problems))
' "${PLAN}"; then
  restore_env
  die "the effective compose configuration does not match the plan; ${ENV_FILE} restored."
fi
VERIFIED_SERVICE="${SERVICE}"
log "Effective configuration matches the plan for ${VERIFIED_SERVICE}"

if ((PLAN_ONLY == 1)); then
  log "PLAN ONLY -- gates 1-3 passed, no container touched." \
    "${ENV_FILE} is restored on exit."
  exit 0
fi

# --- refuse to silently do nothing -----------------------------------------
if ((ENV_CHANGED == 0)) && ((LISTENERS_ALREADY_MATCH == 1)); then
  log "ALREADY RECONCILED: ${VERIFIED_SERVICE} already matches the declaration" \
    "for ${ENVIRONMENT} in both address families. No container was recreated."
  exit 3
fi

# --- gate 4: recreate, and prove the container actually changed ------------
#
# From here on .env is NOT rolled back on failure. The planned value is the
# value the host should hold; reverting it would leave the file disagreeing
# with whatever the container ended up running.
CLEANUP_RESTORE_ENV=0
BEFORE_ID="$("${COMPOSE[@]}" ps -q "${VERIFIED_SERVICE}" 2>/dev/null | head -n 1 || true)"
log "Recreating ${VERIFIED_SERVICE} (currently ${BEFORE_ID:-not running})"
if ! "${COMPOSE[@]}" up -d --timeout "${RECREATE_TIMEOUT_SECONDS}" \
  "${VERIFIED_SERVICE}"; then
  die "recreate of ${VERIFIED_SERVICE} failed. ${ENV_FILE} still holds the
  planned values; the container may still be on its previous definition.
  Inspect it before retrying."
fi

AFTER_ID="$("${COMPOSE[@]}" ps -q "${VERIFIED_SERVICE}" 2>/dev/null | head -n 1 || true)"
[[ -n "${AFTER_ID}" ]] ||
  die "${VERIFIED_SERVICE} is not running after the recreate."
if [[ "${BEFORE_ID}" == "${AFTER_ID}" ]]; then
  die "${VERIFIED_SERVICE} kept container ${AFTER_ID}: compose did not recreate
  it, so the new binding was NOT applied. Refusing to report success."
fi
log "Recreated ${VERIFIED_SERVICE}: ${BEFORE_ID:-none} -> ${AFTER_ID}"

# --- gate 5: read the ACTUAL listeners back, both families ----------------
log "Verifying actual listeners for ${VERIFIED_SERVICE}"
collect_listeners "${AFTER_JSON}"
run_repo_module scripts.published_ports check-listeners \
  --environment "${ENVIRONMENT}" --service "${VERIFIED_SERVICE}" \
  --observed "${AFTER_JSON}" ||
  die "${VERIFIED_SERVICE} was recreated but its actual listeners still do not
  match the declaration. Investigate before assuming this is fixed."

log "RECONCILED: ${VERIFIED_SERVICE} matches the declaration for ${ENVIRONMENT}."
