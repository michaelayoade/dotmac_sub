#!/usr/bin/env bash
# Provider-neutral host adapter for published-port reconciliation v2.
#
# PLAN is read-only: it takes a shared deploy lock, collects only the approved
# secret-free Docker fields plus an in-memory digest of effective Compose, and
# writes artifacts outside the deployment directory. APPLY is a separate mode
# reached only by the separately authorized workflow. It takes the exclusive
# deploy lock, makes an immediate third byte-identical plan, arms a persistent
# root-owned systemd deadman, and then recreates exactly one service.

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DEPLOY_DIR="${DEPLOY_DIR:-}"
ENV_FILE="${DEPLOY_DIR}/.env"
RELEASE_COMPOSE_FILE="${REPO_DIR}/docker-compose.yml"
HOST_COMPOSE_OVERRIDE="${DEPLOY_DIR}/docker-compose.override.yml"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOCK_FILE="${DEPLOY_LOCK_FILE:-/var/lock/dotmac_sub_deploy.lock}"
STATE_ROOT="/var/lib/dotmac/published-port-reconcile"
DEADMAN_BIN="/usr/local/libexec/dotmac-published-port-deadman"
PLAN_OBSERVER_BIN="/usr/local/libexec/dotmac-published-port-plan-observer"
SYSTEMD_DIR="/etc/systemd/system"
PROOF_WAIT_SECONDS="${PUBLISHED_PORT_PROOF_WAIT_SECONDS:-90}"
DOCKER_BIN=""

die() {
  echo "PUBLISHED PORT V2 REFUSED: $*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
usage: reconcile_published_ports_v2.sh plan --service NAME --source-sha SHA \
         --target-server-name dotmac-sub-prod --change-reference REF \
         --reason TEXT --run-id ID --output-dir DIR
       reconcile_published_ports_v2.sh apply --plan FILE --admission FILE \
         --source-sha SHA --apply-run-id ID --output-dir DIR \
         --firewall-proof-dir DIR --reach-proof-dir DIR
       reconcile_published_ports_v2.sh verify-toolchain
EOF
  exit 2
}

run_owner() {
  REPO_DIR="${REPO_DIR}" PYTHON_BIN="${PYTHON_BIN}" PYTHONPATH= \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
    bash "${REPO_DIR}/scripts/run_repo_module.sh" \
    scripts.published_port_reconcile_v2 "$@"
}

env_value() {
  local key="$1"
  grep -E "^${key}=" "${ENV_FILE}" 2>/dev/null | tail -n 1 | cut -d= -f2- || true
}

require_source() {
  [[ -f "${RELEASE_COMPOSE_FILE}" ]] || die "release Compose file is absent"
  [[ "$(git -C "${REPO_DIR}" rev-parse HEAD)" == "${SOURCE_SHA}" ]] ||
    die "Actions checkout is not the exact requested source SHA"
  command -v flock >/dev/null || die "flock is unavailable"
}

require_host() {
  require_source
  [[ -n "${DEPLOY_DIR}" ]] || die "DEPLOY_DIR is required"
  [[ -f "${ENV_FILE}" ]] || die "missing production environment file"
  [[ "$(env_value APP_ENV)" == "production" ]] || die "APP_ENV is not production"
  [[ "$(env_value SERVER_NAME)" == "dotmac-sub-prod" ]] ||
    die "SERVER_NAME is not dotmac-sub-prod"
}

require_root_owned_nonwritable_metadata() {
  local metadata="$1" label="$2" mode
  [[ "${metadata%%:*}" == "0" ]] || die "${label} is not root-owned"
  mode="${metadata#*:}"
  (( (8#${mode} & 0022) == 0 )) || die "${label} is group/world writable"
}

require_python_toolchain() {
  local selected python_stat dependency_output dependency_path dependency_stat
  local -a dependency_paths
  [[ -n "${PYTHON_BIN}" ]] || die "PUBLISHED_PORT_RECONCILE_PYTHON_BIN is required"
  selected="$(command -v "${PYTHON_BIN}")" || die "selected Python is unavailable"
  PYTHON_BIN="$(realpath "${selected}")"
  [[ "${PYTHON_BIN}" == /* && -x "${PYTHON_BIN}" ]] ||
    die "selected Python is not an absolute executable"
  python_stat="$(stat -c '%u:%a' "${PYTHON_BIN}")"
  require_root_owned_nonwritable_metadata "${python_stat}" "selected Python"
  dependency_output="$(PYTHONPATH= PYTHONNOUSERSITE=1 "${PYTHON_BIN}" -I -c \
    'from pathlib import Path; import pydantic, pydantic_core; assert int(pydantic.__version__.split(".", 1)[0]) == 2; print(Path(pydantic.__file__).resolve()); print(Path(pydantic_core.__file__).resolve())')" ||
    die "selected Python does not provide Pydantic v2"
  mapfile -t dependency_paths <<<"${dependency_output}"
  [[ "${#dependency_paths[@]}" -eq 2 ]] ||
    die "Pydantic toolchain did not identify two dependency modules"
  for dependency_path in "${dependency_paths[@]}"; do
    [[ "${dependency_path}" == /* && -f "${dependency_path}" ]] ||
      die "Pydantic dependency path is not an absolute file"
    dependency_stat="$(stat -c '%u:%a' "${dependency_path}")"
    require_root_owned_nonwritable_metadata \
      "${dependency_stat}" "Pydantic dependency"
  done
}

compose_args() {
  COMPOSE=("${DOCKER_BIN}" compose --project-directory "${DEPLOY_DIR}"
    --env-file "${ENV_FILE}" -f "${RELEASE_COMPOSE_FILE}")
  if [[ -f "${HOST_COMPOSE_OVERRIDE}" ]]; then
    COMPOSE+=(-f "${HOST_COMPOSE_OVERRIDE}")
  fi
}

require_root_evidence_dir() {
  local path="$1" label="$2" metadata mode
  [[ -d "${path}" ]] || die "${label} proof directory is absent"
  metadata="$(stat -c '%u:%a' "${path}")"
  [[ "${metadata%%:*}" == "0" ]] || die "${label} proof directory is not root-owned"
  mode="${metadata#*:}"
  (( (8#${mode} & 0022) == 0 )) ||
    die "${label} proof directory is group/world writable"
  [[ -r "${path}" && ! -w "${path}" ]] ||
    die "APPLY identity must read but cannot mint ${label} proofs"
}

collect_inputs() {
  local directory="$1" ids
  mkdir -p "${directory}"
  chmod 0700 "${directory}"
  ids="$("${COMPOSE[@]}" ps -q)"
  [[ -n "${ids}" ]] || die "compose project has no running containers"
  # Exactly seven approved fields leave Docker. Config.Env is deliberately not
  # requested: it contains every application secret.
  # shellcheck disable=SC2086
  "${DOCKER_BIN}" inspect ${ids} --format \
    '{"compose_project":{{json (index .Config.Labels "com.docker.compose.project")}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},"container":{{json .Name}},"container_id":{{json .Id}},"image_id":{{json .Image}},"image_reference":{{json .Config.Image}},"ports":{{json .NetworkSettings.Ports}}}' \
    >"${directory}/containers.jsonl"
  chmod 0600 "${directory}/containers.jsonl"
  "${COMPOSE[@]}" config --format json >"${directory}/effective-compose.json"
  chmod 0600 "${directory}/effective-compose.json"
}

MODE="${1:-}"
[[ -n "${MODE}" ]] || usage
shift

if [[ "${MODE}" == "verify-toolchain" ]]; then
  (($# == 0)) || usage
  require_python_toolchain
  echo "TOOLCHAIN VERIFIED: root-owned Python with Pydantic v2."
  exit 0
fi

SERVICE=""
SOURCE_SHA=""
TARGET_SERVER=""
CHANGE_REFERENCE=""
REASON=""
RUN_ID=""
APPLY_RUN_ID=""
OUTPUT_DIR=""
PLAN_PATH=""
ADMISSION_PATH=""
FIREWALL_PROOF_DIR=""
REACH_PROOF_DIR=""

while (($#)); do
  case "$1" in
    --service) SERVICE="${2:-}"; shift 2 ;;
    --source-sha) SOURCE_SHA="${2:-}"; shift 2 ;;
    --target-server-name) TARGET_SERVER="${2:-}"; shift 2 ;;
    --change-reference) CHANGE_REFERENCE="${2:-}"; shift 2 ;;
    --reason) REASON="${2:-}"; shift 2 ;;
    --run-id) RUN_ID="${2:-}"; shift 2 ;;
    --apply-run-id) APPLY_RUN_ID="${2:-}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:-}"; shift 2 ;;
    --plan) PLAN_PATH="${2:-}"; shift 2 ;;
    --admission) ADMISSION_PATH="${2:-}"; shift 2 ;;
    --firewall-proof-dir) FIREWALL_PROOF_DIR="${2:-}"; shift 2 ;;
    --reach-proof-dir) REACH_PROOF_DIR="${2:-}"; shift 2 ;;
    *) usage ;;
  esac
done

[[ "${SOURCE_SHA}" =~ ^[0-9a-f]{40}$ ]] || usage
[[ -n "${OUTPUT_DIR}" ]] || usage
require_python_toolchain

if [[ "${MODE}" == "plan" ]]; then
  require_source
  [[ -n "${DEPLOY_DIR}" ]] || die "DEPLOY_DIR boundary is required"
  [[ -n "${SERVICE}" && -n "${TARGET_SERVER}" && -n "${CHANGE_REFERENCE}" ]] || usage
  [[ -n "${REASON}" && "${RUN_ID}" =~ ^[1-9][0-9]*$ ]] || usage
  [[ "${TARGET_SERVER}" == "dotmac-sub-prod" ]] || die "target is not dotmac-sub-prod"
  DEPLOY_REAL="$("${PYTHON_BIN}" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "${DEPLOY_DIR}")"
  OUTPUT_REAL="$("${PYTHON_BIN}" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "${OUTPUT_DIR}")"
  [[ "${OUTPUT_REAL}" != "${DEPLOY_REAL}" && "${OUTPUT_REAL}" != "${DEPLOY_REAL}/"* ]] ||
    die "PLAN output must be outside the deployment directory"
  if ! { exec 9<"${LOCK_FILE}"; } 2>/dev/null; then
    die "the existing deploy lock cannot be opened read-only"
  fi
  flock -s -n 9 || die "a deploy or apply holds the deploy lock"
  SCRATCH="$(mktemp -d)"
  trap 'rm -rf -- "${SCRATCH}"' EXIT
  [[ ! -w /var/run/docker.sock ]] ||
    die "PLAN identity holds the Docker mutation credential"
  [[ -x "${PLAN_OBSERVER_BIN}" ]] || die "root-owned PLAN observer is absent"
  OBSERVER_STAT="$(stat -c '%u:%a' "${PLAN_OBSERVER_BIN}")"
  [[ "${OBSERVER_STAT%%:*}" == "0" ]] || die "PLAN observer is not root-owned"
  OBSERVER_MODE="${OBSERVER_STAT#*:}"
  (( (8#${OBSERVER_MODE} & 0022) == 0 )) ||
    die "PLAN observer is group/world writable"
  git -C "${REPO_DIR}" diff --quiet -- scripts/published_port_plan_observer.py ||
    die "PLAN observer source is dirty"
  git -C "${REPO_DIR}" diff --cached --quiet -- \
    scripts/published_port_plan_observer.py || die "PLAN observer source is staged-dirty"
  cmp -s "${REPO_DIR}/scripts/published_port_plan_observer.py" \
    "${PLAN_OBSERVER_BIN}" || die "installed PLAN observer differs from reviewed source"
  sudo -n "${PLAN_OBSERVER_BIN}" collect --service "${SERVICE}" \
    >"${SCRATCH}/host-snapshot.json"
  chmod 0600 "${SCRATCH}/host-snapshot.json"
  run_owner build-plan \
    --service "${SERVICE}" \
    --source-sha "${SOURCE_SHA}" \
    --target-server-name "${TARGET_SERVER}" \
    --change-reference "${CHANGE_REFERENCE}" \
    --reason "${REASON}" \
    --run-id "${RUN_ID}" \
    --planned-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --snapshot "${SCRATCH}/host-snapshot.json" \
    --output-dir "${OUTPUT_DIR}"
  echo "PLAN PRODUCED: read-only published-port decision for ${SERVICE}."
  exit 0
fi

[[ "${MODE}" == "apply" ]] || usage
require_host
DOCKER_BIN="$(command -v docker)" || die "docker is unavailable"
[[ "${DOCKER_BIN}" == /* ]] || die "Docker binary path is not absolute"
DOCKER_STAT="$(stat -c '%u:%a' "${DOCKER_BIN}")"
[[ "${DOCKER_STAT%%:*}" == "0" ]] || die "Docker binary is not root-owned"
DOCKER_MODE="${DOCKER_STAT#*:}"
(( (8#${DOCKER_MODE} & 0022) == 0 )) || die "Docker binary is group/world writable"
compose_args
[[ -f "${PLAN_PATH}" && -f "${ADMISSION_PATH}" ]] || usage
[[ "${APPLY_RUN_ID}" =~ ^[1-9][0-9]*$ ]] || usage
[[ -d "${FIREWALL_PROOF_DIR}" && -d "${REACH_PROOF_DIR}" ]] ||
  die "trusted firewall and external-vantage proof directories must exist"
[[ "${PROOF_WAIT_SECONDS}" =~ ^[1-9][0-9]*$ && "${PROOF_WAIT_SECONDS}" -le 300 ]] ||
  die "proof wait must be between 1 and 300 seconds"
[[ "$(realpath "${FIREWALL_PROOF_DIR}")" != "$(realpath "${REACH_PROOF_DIR}")" ]] ||
  die "firewall and external reach proofs require independent stores"
require_root_evidence_dir "${FIREWALL_PROOF_DIR}" firewall
require_root_evidence_dir "${REACH_PROOF_DIR}" external-reach

if ! { exec 9>"${LOCK_FILE}"; } 2>/dev/null; then
  die "cannot open the deploy lock"
fi
flock -n 9 || die "another deploy or reconcile holds the deploy lock"

SCRATCH="$(mktemp -d)"
ARMED=0
OPERATION_ID=""
cleanup() {
  local status="$?"
  if ((ARMED == 1)); then
    sudo -n "${DEADMAN_BIN}" rollback-now --operation "${OPERATION_ID}" \
      --reason postcondition-failure || true
  fi
  rm -rf -- "${SCRATCH}"
  exit "${status}"
}
signal_exit() {
  if ((ARMED == 1)); then
    if sudo -n "${DEADMAN_BIN}" rollback-now --operation "${OPERATION_ID}" \
      --reason signal; then
      ARMED=0
    fi
  fi
  exit 130
}
trap cleanup EXIT
trap signal_exit INT TERM HUP

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
run_owner verify-admission --admission "${ADMISSION_PATH}" --plan "${PLAN_PATH}" \
  --source-sha "${SOURCE_SHA}" --apply-run-id "${APPLY_RUN_ID}" --now "${NOW}"

# Immediate third plan, under the exclusive deploy lock and before any write.
collect_inputs "${SCRATCH}/third"
run_owner build-immediate-plan --basis-plan "${PLAN_PATH}" \
  --effective-compose "${SCRATCH}/third/effective-compose.json" \
  --containers "${SCRATCH}/third/containers.jsonl" \
  --output "${SCRATCH}/third-plan.json"
run_owner verify-third-plan --admission "${ADMISSION_PATH}" \
  --admitted-plan "${PLAN_PATH}" --immediate-plan "${SCRATCH}/third-plan.json" \
  --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

SERVICE="$("${PYTHON_BIN}" -c 'import json,sys; print(json.load(open(sys.argv[1]))["plan"]["intent"]["service"])' "${PLAN_PATH}")"
OPERATION_ID="port-${SERVICE}-${APPLY_RUN_ID}"
STATE_DIR="${STATE_ROOT}/${OPERATION_ID}"
[[ "${DEPLOY_DIR}" != *$'\n'* && "${DEPLOY_DIR}" != *' '* ]] ||
  die "deployment path cannot be represented safely in the systemd drop-in"

# Build the persistent rollback bundle before arming. Its Compose inputs and
# deadman executable survive runner death and checkout cleanup.
sudo -n install -d -o root -g root -m 0700 "${STATE_DIR}"
sudo -n install -o root -g root -m 0750 \
  "${REPO_DIR}/scripts/published_port_deadman.py" "${DEADMAN_BIN}"
sudo -n install -o root -g root -m 0644 \
  "${REPO_DIR}/deploy/systemd/dotmac-published-port-deadman@.service" \
  "${SYSTEMD_DIR}/dotmac-published-port-deadman@.service"
sudo -n install -o root -g root -m 0644 \
  "${REPO_DIR}/deploy/systemd/dotmac-published-port-deadman@.timer" \
  "${SYSTEMD_DIR}/dotmac-published-port-deadman@.timer"
sudo -n install -o root -g root -m 0600 "${RELEASE_COMPOSE_FILE}" \
  "${STATE_DIR}/release-compose.yml"
COMPOSE_FILES=("${STATE_DIR}/release-compose.yml")
if [[ -f "${HOST_COMPOSE_OVERRIDE}" ]]; then
  sudo -n install -o root -g root -m 0600 "${HOST_COMPOSE_OVERRIDE}" \
    "${STATE_DIR}/host-override.yml"
  COMPOSE_FILES+=("${STATE_DIR}/host-override.yml")
fi
run_owner write-image-pin --plan "${PLAN_PATH}" --output "${SCRATCH}/image-pin.json"
sudo -n install -o root -g root -m 0600 "${SCRATCH}/image-pin.json" \
  "${STATE_DIR}/image-pin.json"
COMPOSE_FILES+=("${STATE_DIR}/image-pin.json")
RUNTIME_COMPOSE_FILES=("${RELEASE_COMPOSE_FILE}")
if [[ -f "${HOST_COMPOSE_OVERRIDE}" ]]; then
  RUNTIME_COMPOSE_FILES+=("${HOST_COMPOSE_OVERRIDE}")
fi
RUNTIME_COMPOSE_FILES+=("${SCRATCH}/image-pin.json")

DEADLINE="$("${PYTHON_BIN}" -c 'from datetime import UTC,datetime,timedelta; print((datetime.now(UTC)+timedelta(minutes=5)).isoformat().replace("+00:00","Z"))')"
PREPARE=(prepare-deadman --admission "${ADMISSION_PATH}" --plan "${PLAN_PATH}"
  --env-file "${ENV_FILE}" --docker-bin "${DOCKER_BIN}"
  --deploy-dir "${DEPLOY_DIR}" --deadline "${DEADLINE}"
  --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --output "${SCRATCH}/state.json")
for compose_file in "${COMPOSE_FILES[@]}"; do
  PREPARE+=(--compose-file "${compose_file}")
done
run_owner "${PREPARE[@]}"
sudo -n install -o root -g root -m 0600 "${SCRATCH}/state.json" "${STATE_DIR}/state.json"

DROPIN_DIR="${SYSTEMD_DIR}/dotmac-published-port-deadman@${OPERATION_ID}.service.d"
cat >"${SCRATCH}/override.conf" <<EOF
[Service]
ReadWritePaths=${ENV_FILE} ${STATE_DIR} /var/run/docker.sock
EOF
sudo -n install -d -o root -g root -m 0755 "${DROPIN_DIR}"
sudo -n install -o root -g root -m 0644 "${SCRATCH}/override.conf" \
  "${DROPIN_DIR}/override.conf"
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now "dotmac-published-port-deadman@${OPERATION_ID}.timer"
sudo -n "${DEADMAN_BIN}" validate --operation "${OPERATION_ID}"
sudo -n systemctl is-active --quiet "dotmac-published-port-deadman@${OPERATION_ID}.timer"
ARMED=1

# Point of no return. The persistent deadman is already active.
run_owner apply-env --admission "${ADMISSION_PATH}" --plan "${PLAN_PATH}" \
  --env-file "${ENV_FILE}" --source-sha "${SOURCE_SHA}" \
  --apply-run-id "${APPLY_RUN_ID}" --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

APPLY_COMPOSE=("${DOCKER_BIN}" compose --project-directory "${DEPLOY_DIR}" --env-file "${ENV_FILE}")
for compose_file in "${RUNTIME_COMPOSE_FILES[@]}"; do
  APPLY_COMPOSE+=(-f "${compose_file}")
done
"${APPLY_COMPOSE[@]}" config --format json >"${SCRATCH}/effective-before-up.json"
run_owner verify-effective --plan "${PLAN_PATH}" \
  --effective-compose "${SCRATCH}/effective-before-up.json"
"${APPLY_COMPOSE[@]}" up -d --no-deps --no-build --pull never --force-recreate \
  "${SERVICE}"

COMPOSE=("${APPLY_COMPOSE[@]}")
collect_inputs "${SCRATCH}/after"

# External collectors write canonical, non-secret receipts under an operation
# directory. The target host cannot mint its own reachability evidence.
FIREWALL_OPERATION_DIR="${FIREWALL_PROOF_DIR}/${OPERATION_ID}"
REACH_OPERATION_DIR="${REACH_PROOF_DIR}/${OPERATION_ID}"
deadline_epoch="$(( $(date +%s) + PROOF_WAIT_SECONDS ))"
while [[ ! -d "${FIREWALL_OPERATION_DIR}" || ! -d "${REACH_OPERATION_DIR}" ]]; do
  (( $(date +%s) < deadline_epoch )) || die "required client proofs did not arrive"
  sleep 2
done
mapfile -d '' FIREWALL_PROOFS < <(find "${FIREWALL_OPERATION_DIR}" -maxdepth 1 -type f -name '*.json' -print0 | sort -z)
mapfile -d '' REACH_PROOFS < <(find "${REACH_OPERATION_DIR}" -maxdepth 1 -type f -name '*.json' -print0 | sort -z)
VERIFY=(verify-postconditions --admission "${ADMISSION_PATH}" --plan "${PLAN_PATH}"
  --effective-compose "${SCRATCH}/after/effective-compose.json"
  --containers "${SCRATCH}/after/containers.jsonl"
  --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --output "${SCRATCH}/verdict.json")
for proof in "${FIREWALL_PROOFS[@]}"; do VERIFY+=(--firewall-proof "${proof}"); done
for proof in "${REACH_PROOFS[@]}"; do VERIFY+=(--reach-proof "${proof}"); done
run_owner "${VERIFY[@]}"

sudo -n "${DEADMAN_BIN}" disarm --operation "${OPERATION_ID}"
ARMED=0
sudo -n systemctl disable --now "dotmac-published-port-deadman@${OPERATION_ID}.timer"
sudo -n cat "${STATE_DIR}/state.json" >"${SCRATCH}/disarmed-state.json"
run_owner finalize-outcome --verdict "${SCRATCH}/verdict.json" \
  --deadman-state "${SCRATCH}/disarmed-state.json" \
  --disarmed-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --output "${OUTPUT_DIR}/outcome.json"

trap - EXIT INT TERM HUP
rm -rf -- "${SCRATCH}"
echo "APPLIED: ${SERVICE}; target-only recreate proved and deadman disarmed."
