#!/usr/bin/env bash
# Host adapter for the structurally single-use legacy image-pin bootstrap.
#
# PLAN is read-only. It takes a SHARED deploy lock, runs the installed
# root-owned bootstrap observer, and writes artifacts outside the deployment
# directory.
#
# APPLY is reached only from the separately authorized production workflow. It
# takes the EXCLUSIVE deploy lock, re-observes the complete prestate under that
# lock, requires byte identity with both admitted plans, proves replication is
# streaming, proves the desired digest resolves locally to the running image ID,
# builds a root-owned rollback bundle, arms a persistent systemd deadman, and
# only then performs its single mutation.
#
# Every decision lives in scripts/legacy_image_pin_bootstrap.py. This file
# collects files, holds locks, and runs Docker.

set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DEPLOY_DIR="${DEPLOY_DIR:-}"
ENV_FILE="${DEPLOY_DIR}/.env"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LOCK_FILE="${DEPLOY_LOCK_FILE:-/var/lock/dotmac_sub_deploy.lock}"
STATE_ROOT="/var/lib/dotmac/legacy-image-pin"
RECEIPT_PATH="${STATE_ROOT}/receipt.json"
OBSERVER_BIN="/usr/local/libexec/dotmac-legacy-image-pin-observer"
DEADMAN_BIN="/usr/local/libexec/dotmac-legacy-image-pin-deadman"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE="postgres-local"
PROOF_WAIT_SECONDS="${LEGACY_IMAGE_PIN_PROOF_WAIT_SECONDS:-120}"
REPLICATION_WAIT_SECONDS="${LEGACY_IMAGE_PIN_REPLICATION_WAIT_SECONDS:-90}"
DOCKER_BIN=""

die() {
  echo "LEGACY IMAGE PIN BOOTSTRAP REFUSED: $*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
usage: legacy_image_pin_bootstrap.sh plan --source-sha SHA \
         --change-reference REF --reason TEXT --run-id ID --output-dir DIR
       legacy_image_pin_bootstrap.sh apply --plan FILE --admission FILE \
         --source-sha SHA --apply-run-id ID --output-dir DIR \
         --firewall-proof-dir DIR --reach-proof-dir DIR
EOF
  exit 2
}

run_owner() {
  REPO_DIR="${REPO_DIR}" PYTHON_BIN="${PYTHON_BIN}" PYTHONPATH= \
    PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 \
    bash "${REPO_DIR}/scripts/run_repo_module.sh" \
    scripts.legacy_image_pin_bootstrap "$@"
}

env_value() {
  local key="$1"
  grep -E "^${key}=" "${ENV_FILE}" 2>/dev/null | tail -n 1 | cut -d= -f2- || true
}

require_root_owned_nonwritable() {
  local path="$1" label="$2" metadata mode
  metadata="$(stat -c '%u:%a' "${path}")"
  [[ "${metadata%%:*}" == "0" ]] || die "${label} is not root-owned"
  mode="${metadata#*:}"
  (( (8#${mode} & 0022) == 0 )) || die "${label} is group/world writable"
}

require_source() {
  [[ "$(git -C "${REPO_DIR}" rev-parse HEAD)" == "${SOURCE_SHA}" ]] ||
    die "Actions checkout is not the exact requested source SHA"
  command -v flock >/dev/null || die "flock is unavailable"
}

# The single-use gate, asserted before anything else happens on either lane.
require_single_use() {
  [[ ! -e "${RECEIPT_PATH}" ]] ||
    die "a terminal legacy image-pin receipt exists; this bootstrap is single-use"
}

require_installed_observer() {
  [[ -x "${OBSERVER_BIN}" ]] || die "the root-owned bootstrap observer is absent"
  require_root_owned_nonwritable "${OBSERVER_BIN}" "bootstrap observer"
  git -C "${REPO_DIR}" diff --quiet -- scripts/legacy_image_pin_observer.py ||
    die "bootstrap observer source is dirty"
  git -C "${REPO_DIR}" diff --cached --quiet -- \
    scripts/legacy_image_pin_observer.py ||
    die "bootstrap observer source is staged-dirty"
  cmp -s "${REPO_DIR}/scripts/legacy_image_pin_observer.py" "${OBSERVER_BIN}" ||
    die "the installed bootstrap observer differs from the reviewed source"
}

MODE="${1:-}"
[[ -n "${MODE}" ]] || usage
shift

SOURCE_SHA=""
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
    --source-sha) SOURCE_SHA="${2:-}"; shift 2 ;;
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
[[ -n "${OUTPUT_DIR}" && -n "${DEPLOY_DIR}" ]] || usage
require_single_use

if [[ "${MODE}" == "plan" ]]; then
  require_source
  [[ -n "${CHANGE_REFERENCE}" && -n "${REASON}" ]] || usage
  [[ "${RUN_ID}" =~ ^[1-9][0-9]*$ ]] || usage
  DEPLOY_REAL="$("${PYTHON_BIN}" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "${DEPLOY_DIR}")"
  OUTPUT_REAL="$("${PYTHON_BIN}" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "${OUTPUT_DIR}")"
  [[ "${OUTPUT_REAL}" != "${DEPLOY_REAL}" && "${OUTPUT_REAL}" != "${DEPLOY_REAL}/"* ]] ||
    die "PLAN output must be outside the deployment directory"
  if ! { exec 9<"${LOCK_FILE}"; } 2>/dev/null; then
    die "the existing deploy lock cannot be opened read-only"
  fi
  flock -s -n 9 || die "a deploy or apply holds the deploy lock"
  [[ ! -w /var/run/docker.sock ]] ||
    die "PLAN identity holds the Docker mutation credential"
  require_installed_observer
  SCRATCH="$(mktemp -d)"
  trap 'rm -rf -- "${SCRATCH}"' EXIT
  sudo -n "${OBSERVER_BIN}" collect >"${SCRATCH}/snapshot.json"
  chmod 0600 "${SCRATCH}/snapshot.json"
  run_owner build-plan \
    --snapshot "${SCRATCH}/snapshot.json" \
    --source-sha "${SOURCE_SHA}" \
    --change-reference "${CHANGE_REFERENCE}" \
    --reason "${REASON}" \
    --planned-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --run-id "${RUN_ID}" \
    --output-dir "${OUTPUT_DIR}"
  echo "PLAN PRODUCED: read-only legacy image-pin bootstrap decision."
  exit 0
fi

[[ "${MODE}" == "apply" ]] || usage
[[ -f "${PLAN_PATH}" && -f "${ADMISSION_PATH}" ]] || usage
[[ "${APPLY_RUN_ID}" =~ ^[1-9][0-9]*$ ]] || usage
require_source
[[ -f "${ENV_FILE}" ]] || die "missing production environment file"
[[ "$(env_value APP_ENV)" == "production" ]] || die "APP_ENV is not production"
[[ "$(env_value SERVER_NAME)" == "dotmac-sub-prod" ]] ||
  die "SERVER_NAME is not dotmac-sub-prod"
[[ -d "${FIREWALL_PROOF_DIR}" && -d "${REACH_PROOF_DIR}" ]] ||
  die "trusted firewall and external-vantage proof directories must exist"
[[ "$(realpath "${FIREWALL_PROOF_DIR}")" != "$(realpath "${REACH_PROOF_DIR}")" ]] ||
  die "firewall and external reach proofs require independent stores"
require_root_owned_nonwritable "${FIREWALL_PROOF_DIR}" "firewall proof directory"
require_root_owned_nonwritable "${REACH_PROOF_DIR}" "external-reach proof directory"
[[ -r "${FIREWALL_PROOF_DIR}" && ! -w "${FIREWALL_PROOF_DIR}" ]] ||
  die "APPLY identity must read but cannot mint firewall proofs"
[[ -r "${REACH_PROOF_DIR}" && ! -w "${REACH_PROOF_DIR}" ]] ||
  die "APPLY identity must read but cannot mint external-reach proofs"
require_installed_observer

DOCKER_BIN="$(command -v docker)" || die "docker is unavailable"
[[ "${DOCKER_BIN}" == /* ]] || die "Docker binary path is not absolute"
require_root_owned_nonwritable "${DOCKER_BIN}" "Docker binary"

# The exclusive lock is taken BEFORE re-observation, not after: an observation
# made outside the lock could be invalidated by a concurrent deploy between the
# observation and the mutation, which is exactly the race the byte-identity
# check exists to close.
if ! { exec 9>"${LOCK_FILE}"; } 2>/dev/null; then
  die "cannot open the deploy lock"
fi
flock -n 9 || die "another deploy or reconcile holds the deploy lock"
require_single_use

OPERATION_ID="imagepin-${SERVICE}-${APPLY_RUN_ID}"
STATE_DIR="${STATE_ROOT}/${OPERATION_ID}"
SCRATCH="$(mktemp -d)"
chmod 0700 "${SCRATCH}"
ARMED=0
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

# 1. Re-observe the complete prestate under the exclusive lock.
sudo -n "${OBSERVER_BIN}" collect >"${SCRATCH}/prestate.json"
chmod 0600 "${SCRATCH}/prestate.json"

# 2. Replication must already be streaming. Recreating a target whose standby
#    is already broken would attribute an existing fault to this change.
CONTAINER_ID="$(sudo -n "${DOCKER_BIN}" ps -q --no-trunc \
  --filter "label=com.docker.compose.project=dotmac_sub" \
  --filter "label=com.docker.compose.service=${SERVICE}")"
[[ -n "${CONTAINER_ID}" ]] || die "the target container is not running"
# The standby's walreceiver does not reconnect instantly after a recreate, and
# Postgres itself takes a moment to accept connections. Probing once would make
# a healthy window look like a failure and trigger a needless rollback, so the
# probe waits -- bounded, and still fails closed when the deadline passes.
probe_replication() {
  local phase="$1" observed="" target="" wait_deadline
  wait_deadline="$(( $(date +%s) + REPLICATION_WAIT_SECONDS ))"
  while :; do
    target="$(sudo -n "${DOCKER_BIN}" ps -q --no-trunc \
      --filter "label=com.docker.compose.project=dotmac_sub" \
      --filter "label=com.docker.compose.service=${SERVICE}")"
    if [[ -n "${target}" ]] &&
       sudo -n "${DOCKER_BIN}" exec "${target}" pg_isready -U postgres >/dev/null 2>&1; then
      observed="$(sudo -n "${DOCKER_BIN}" exec "${target}" psql -U postgres -tAc \
        "select client_addr || ' ' || state from pg_stat_replication \
         where state = 'streaming'" 2>/dev/null || true)"
      [[ -z "${observed}" ]] || break
    fi
    (( $(date +%s) < wait_deadline )) ||
      die "replication did not reach streaming within ${REPLICATION_WAIT_SECONDS}s (${phase})"
    sleep 2
  done
  "${PYTHON_BIN}" - "$phase" "$OPERATION_ID" "$observed" \
    >"${SCRATCH}/replication-${phase}.json" <<'PY'
import json, sys
from datetime import UTC, datetime

phase, operation, observed = sys.argv[1], sys.argv[2], sys.argv[3].strip()
client_addr, _, state = observed.partition(" ")
document = {
    "schema": "LegacyImagePinReplicationProbeV1",
    "operation_id": operation,
    "phase": phase,
    "state": state.strip(),
    "client_addr": client_addr.strip(),
    "observed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
}
sys.stdout.write(
    json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    + "\n"
)
PY
  chmod 0600 "${SCRATCH}/replication-${phase}.json"
}
probe_replication prestate

# 3. Require byte identity with both admitted plans and the live prestate.
run_owner verify-prestate --admission "${ADMISSION_PATH}" --plan "${PLAN_PATH}" \
  --snapshot "${SCRATCH}/prestate.json" \
  --replication-probe "${SCRATCH}/replication-prestate.json" \
  --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# 4. Prove the desired digest resolves LOCALLY to the running image ID. No
#    pull, no build: docker image inspect only reads what is already here.
DESIRED_REFERENCE="$("${PYTHON_BIN}" -c 'import json,sys; print(json.load(open(sys.argv[1]))["desired_image_reference"])' "${PLAN_PATH}")"
RUNNING_IMAGE_ID="$(sudo -n "${DOCKER_BIN}" inspect "${CONTAINER_ID}" --format '{{.Image}}')"
RESOLVED_IMAGE_ID="$(sudo -n "${DOCKER_BIN}" image inspect "${DESIRED_REFERENCE}" --format '{{.Id}}')"
[[ "${RESOLVED_IMAGE_ID}" == "${RUNNING_IMAGE_ID}" ]] ||
  die "the desired digest does not resolve locally to the running image ID"

# 5. Root-owned rollback bundle. Its Compose inputs and deadman executable
#    survive runner death, checkout cleanup and reboot.
sudo -n install -d -o root -g root -m 0700 "${STATE_ROOT}"
sudo -n install -d -o root -g root -m 0700 "${STATE_DIR}"
sudo -n install -o root -g root -m 0750 \
  "${REPO_DIR}/scripts/legacy_image_pin_deadman.py" "${DEADMAN_BIN}"
sudo -n install -o root -g root -m 0644 \
  "${REPO_DIR}/deploy/systemd/dotmac-legacy-image-pin-deadman@.service" \
  "${SYSTEMD_DIR}/dotmac-legacy-image-pin-deadman@.service"
sudo -n install -o root -g root -m 0644 \
  "${REPO_DIR}/deploy/systemd/dotmac-legacy-image-pin-deadman@.timer" \
  "${SYSTEMD_DIR}/dotmac-legacy-image-pin-deadman@.timer"

COMPOSE_SOURCES=("${DEPLOY_DIR}/docker-compose.yml")
[[ -f "${DEPLOY_DIR}/docker-compose.override.yml" ]] &&
  COMPOSE_SOURCES+=("${DEPLOY_DIR}/docker-compose.override.yml")
BUNDLE_FILES=()
index=0
for source in "${COMPOSE_SOURCES[@]}"; do
  sudo -n install -o root -g root -m 0600 "${source}" \
    "${STATE_DIR}/compose-${index}.yml"
  BUNDLE_FILES+=("${STATE_DIR}/compose-${index}.yml")
  index=$((index + 1))
done
run_owner write-image-pin --plan "${PLAN_PATH}" --output "${SCRATCH}/image-pin.json"
sudo -n install -o root -g root -m 0600 "${SCRATCH}/image-pin.json" \
  "${STATE_DIR}/image-pin.json"
BUNDLE_FILES+=("${STATE_DIR}/image-pin.json")

DEADLINE="$("${PYTHON_BIN}" -c 'from datetime import UTC,datetime,timedelta; print((datetime.now(UTC)+timedelta(minutes=5)).isoformat().replace("+00:00","Z"))')"
PREPARE=(prepare-deadman --admission "${ADMISSION_PATH}" --plan "${PLAN_PATH}"
  --env-file "${ENV_FILE}" --docker-bin "${DOCKER_BIN}" --deploy-dir "${DEPLOY_DIR}"
  --deadline "${DEADLINE}" --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  --output "${SCRATCH}/state.json")
for compose_file in "${BUNDLE_FILES[@]}"; do
  PREPARE+=(--compose-file "${compose_file}")
done
run_owner "${PREPARE[@]}"
sudo -n install -o root -g root -m 0600 "${SCRATCH}/state.json" \
  "${STATE_DIR}/state.json"

DROPIN_DIR="${SYSTEMD_DIR}/dotmac-legacy-image-pin-deadman@${OPERATION_ID}.service.d"
cat >"${SCRATCH}/override.conf" <<EOF
[Service]
ReadWritePaths=${ENV_FILE} ${STATE_DIR} ${STATE_ROOT} /var/run/docker.sock
EOF
sudo -n install -d -o root -g root -m 0755 "${DROPIN_DIR}"
sudo -n install -o root -g root -m 0644 "${SCRATCH}/override.conf" \
  "${DROPIN_DIR}/override.conf"
sudo -n systemctl daemon-reload
sudo -n systemctl enable --now "dotmac-legacy-image-pin-deadman@${OPERATION_ID}.timer"
sudo -n "${DEADMAN_BIN}" validate --operation "${OPERATION_ID}"
sudo -n systemctl is-active --quiet \
  "dotmac-legacy-image-pin-deadman@${OPERATION_ID}.timer"
ARMED=1

# ---------------------------------------------------------------------------
# Point of no return. The persistent deadman is already active.
#
# THE ONLY MUTATION: set the declared bind, add the digest-pinned image overlay
# and force-recreate exactly one service.
# ---------------------------------------------------------------------------
"${PYTHON_BIN}" - "${ENV_FILE}" <<'PY'
import os, sys
from pathlib import Path

path = Path(sys.argv[1])
stat = path.stat()
rows = [
    line
    for line in path.read_text(encoding="utf-8").splitlines()
    if not line.startswith("PG_LOCAL_BIND=")
]
rows.append("PG_LOCAL_BIND=0.0.0.0:")
temporary = path.with_name(f".{path.name}.imagepin-{os.getpid()}")
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.st_mode & 0o777)
with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
    stream.write("\n".join(rows) + "\n")
    stream.flush()
    os.fsync(stream.fileno())
os.chown(temporary, stat.st_uid, stat.st_gid)
os.replace(temporary, path)
PY

APPLY_COMPOSE=("${DOCKER_BIN}" compose --project-name dotmac_sub
  --project-directory "${DEPLOY_DIR}" --env-file "${ENV_FILE}")
for source in "${COMPOSE_SOURCES[@]}"; do
  APPLY_COMPOSE+=(-f "${source}")
done
APPLY_COMPOSE+=(-f "${SCRATCH}/image-pin.json")

"${APPLY_COMPOSE[@]}" up -d --no-deps --no-build --pull never --force-recreate \
  "${SERVICE}"

# ---------------------------------------------------------------------------
# Postconditions.
# ---------------------------------------------------------------------------
AFTER_ID="$(sudo -n "${DOCKER_BIN}" ps -q --no-trunc \
  --filter "label=com.docker.compose.project=dotmac_sub" \
  --filter "label=com.docker.compose.service=${SERVICE}")"
[[ -n "${AFTER_ID}" ]] || die "the recreated target container is not running"
probe_replication poststate

"${PYTHON_BIN}" - "${DOCKER_BIN}" "${AFTER_ID}" "${SERVICE}" "${DEPLOY_DIR}" \
  "${ENV_FILE}" "${SCRATCH}/image-pin.json" "${COMPOSE_SOURCES[@]}" \
  >"${SCRATCH}/poststate.json" <<'PY'
import hashlib, ipaddress, json, subprocess, sys

docker_bin, container, service, deploy_dir, env_file, pin, *sources = sys.argv[1:]
inspected = json.loads(
    subprocess.run(
        [docker_bin, "inspect", container, "--format",
         '{"container_id":{{json .Id}},"image_id":{{json .Image}},"ports":{{json .NetworkSettings.Ports}}}'],
        check=True, capture_output=True, text=True,
    ).stdout
)
listeners = []
for spec, bindings in (inspected["ports"] or {}).items():
    port, _, protocol = spec.partition("/")
    for binding in bindings or ():
        listeners.append({
            "container_port": int(port),
            "host_ip": str(ipaddress.ip_address(binding["HostIp"])),
            "host_port": int(binding["HostPort"]),
            "protocol": protocol,
        })
listeners.sort(key=lambda row: (row["container_port"], row["host_ip"], row["host_port"], row["protocol"]))

compose = [docker_bin, "compose", "--project-name", "dotmac_sub",
           "--project-directory", deploy_dir, "--env-file", env_file]
for source in [*sources, pin]:
    compose.extend(("-f", source))
rendered = json.loads(
    subprocess.run([*compose, "config", "--format", "json"], check=True,
                   capture_output=True, text=True).stdout
)
definition = dict(rendered["services"][service])
definition.pop("ports", None)
effective_image = definition.get("image")
definition.pop("image", None)


def digest(value):
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


rows = subprocess.run(
    [docker_bin, "ps", "-q", "--filter", "label=com.docker.compose.project=dotmac_sub"],
    check=True, capture_output=True, text=True,
).stdout.split()
non_targets = []
for row in json.loads("[" + ",".join(
    subprocess.run(
        [docker_bin, "inspect", *rows, "--format",
         '{"service":{{json (index .Config.Labels "com.docker.compose.service")}},"container":{{json .Name}},"container_id":{{json .Id}}}'],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
) + "]"):
    if row["service"] == service:
        continue
    non_targets.append({
        "service": row["service"],
        "container": row["container"].lstrip("/"),
        "container_id": row["container_id"].removeprefix("sha256:"),
    })
non_targets.sort(key=lambda item: (item["service"], item["container"], item["container_id"]))

sys.stdout.write(json.dumps({
    "target_container_id": inspected["container_id"].removeprefix("sha256:"),
    "image_id": inspected["image_id"],
    "effective_image_reference": effective_image,
    "listeners": listeners,
    "image_free_definition_digest": digest(definition),
    "non_targets": non_targets,
}, sort_keys=True) + "\n")
PY
chmod 0600 "${SCRATCH}/poststate.json"

FIREWALL_OPERATION_DIR="${FIREWALL_PROOF_DIR}/${OPERATION_ID}"
REACH_OPERATION_DIR="${REACH_PROOF_DIR}/${OPERATION_ID}"
deadline_epoch="$(( $(date +%s) + PROOF_WAIT_SECONDS ))"
while [[ ! -f "${FIREWALL_OPERATION_DIR}/proof.json" ||
         ! -f "${REACH_OPERATION_DIR}/proof.json" ]]; do
  (( $(date +%s) < deadline_epoch )) || die "required external proofs did not arrive"
  sleep 2
done

run_owner verify-postconditions --admission "${ADMISSION_PATH}" \
  --plan "${PLAN_PATH}" --poststate "${SCRATCH}/poststate.json" \
  --firewall-proof "${FIREWALL_OPERATION_DIR}/proof.json" \
  --reach-proof "${REACH_OPERATION_DIR}/proof.json" \
  --replication-probe "${SCRATCH}/replication-poststate.json" \
  --now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --output "${OUTPUT_DIR}/verdict.json"

sudo -n "${DEADMAN_BIN}" disarm --operation "${OPERATION_ID}"
ARMED=0
sudo -n systemctl disable --now \
  "dotmac-legacy-image-pin-deadman@${OPERATION_ID}.timer"

# The terminal receipt is written LAST and permanently refuses another
# bootstrap on this host.
AFTER_IMAGE_ID="$(sudo -n "${DOCKER_BIN}" inspect "${AFTER_ID}" --format '{{.Image}}')"
run_owner write-receipt --admission "${ADMISSION_PATH}" --plan "${PLAN_PATH}" \
  --outcome applied --after-container-id "${AFTER_ID}" \
  --image-id "${AFTER_IMAGE_ID}" \
  --recorded-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --output "${SCRATCH}/receipt.json"
sudo -n install -o root -g root -m 0600 "${SCRATCH}/receipt.json" "${RECEIPT_PATH}"
install -m 0600 "${SCRATCH}/receipt.json" "${OUTPUT_DIR}/receipt.json"

trap - EXIT INT TERM HUP
rm -rf -- "${SCRATCH}"
echo "APPLIED: ${SERVICE} pinned to its running digest; bootstrap is now spent."
