#!/usr/bin/env bash
# One-time staged rollout. Build first; only then recreate PostgreSQL, initialize
# the stanza, take a full online backup, and switch deploys to the pgBackRest gate.
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"
DB_CONTAINER="${DB_CONTAINER:-dotmac_pg_local}"
DB_DATA_DIR="${PGBACKREST_DB_DATA_DIR:-/var/lib/dotmac-pg-local}"
POSTGRES_IMAGE="${POSTGRES_IMAGE:-dotmac/postgis-pgbackrest:16-3.4-2.58}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root so backup directories, Docker, and systemd can be configured" >&2
  exit 1
fi
if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  exit 1
fi
for command_name in docker flock; do
  command -v "${command_name}" >/dev/null || { echo "Missing required command: ${command_name}" >&2; exit 1; }
done

set -a
# shellcheck disable=SC1090
. "${ENV_FILE}"
set +a
cd "${ROOT_DIR}"
COMPOSE=(docker compose -f docker-compose.yml)

wait_for_database() {
  local phase="$1"
  local deadline=$((SECONDS + ${PGBACKREST_DB_START_TIMEOUT_SECONDS:-180}))
  until docker exec "${DB_CONTAINER}" pg_isready -U postgres >/dev/null 2>&1; do
    if ((SECONDS >= deadline)); then
      "${COMPOSE[@]}" logs --tail 200 postgres-local >&2 || true
      echo "PostgreSQL did not become ready during ${phase}" >&2
      exit 1
    fi
    sleep 3
  done
}

db_bytes="$(du -sb "${DB_DATA_DIR}" | cut -f1)"
available_bytes="$(df -B1 --output=avail "${DB_DATA_DIR}" | tail -1 | tr -d ' ')"
required_bytes=$((db_bytes * 2))
if ((available_bytes < required_bytes)); then
  echo "Insufficient disk for backup plus restore drill: available=${available_bytes} required=${required_bytes}" >&2
  exit 1
fi

echo "==> Bootstrapping encrypted pgBackRest repository configuration"
bash "${ROOT_DIR}/scripts/backup/bootstrap_pgbackrest.sh"

echo "==> Building backup-enabled PostgreSQL image before touching the live container"
POSTGRES_IMAGE="${POSTGRES_IMAGE}" "${COMPOSE[@]}" build postgres-local
docker run --rm "${POSTGRES_IMAGE}" pgbackrest version

echo "==> Recreating PostgreSQL with WAL archiving initially disabled"
POSTGRES_ARCHIVE_MODE=off POSTGRES_IMAGE="${POSTGRES_IMAGE}" \
  "${COMPOSE[@]}" up -d --no-deps --force-recreate postgres-local
wait_for_database "backup image activation"

echo "==> Creating stanza before WAL archiving is enabled"
docker exec --user postgres "${DB_CONTAINER}" pgbackrest --stanza="${PGBACKREST_STANZA:-dotmac-sub}" stanza-create

echo "==> Restarting PostgreSQL with WAL archiving enabled"
POSTGRES_ARCHIVE_MODE=on POSTGRES_IMAGE="${POSTGRES_IMAGE}" \
  "${COMPOSE[@]}" up -d --no-deps --force-recreate postgres-local
wait_for_database "WAL archive activation"

echo "==> Proving WAL archive transport"
docker exec --user postgres "${DB_CONTAINER}" pgbackrest --stanza="${PGBACKREST_STANZA:-dotmac-sub}" check

echo "==> Taking first online full backup (the app remains online)"
bash "${ROOT_DIR}/scripts/backup/pgbackrest_backup.sh" full
bash "${ROOT_DIR}/scripts/backup/pgbackrest_health.sh" --gate

set_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "${ENV_FILE}"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
  fi
}
set_env_value POSTGRES_IMAGE "${POSTGRES_IMAGE}"
set_env_value BACKUP_MODE pgbackrest

echo "==> Installing backup and health timers"
bash "${ROOT_DIR}/scripts/backup/install_pgbackrest_systemd.sh"
"${COMPOSE[@]}" up -d promtail

if [[ "${RUN_RESTORE_VERIFY:-0}" == "1" ]]; then
  echo "==> Running first isolated restore verification"
  bash "${ROOT_DIR}/scripts/backup/pgbackrest_restore_verify.sh" --force
else
  echo "Restore drill not run. Execute with RUN_RESTORE_VERIFY=1 or run pgbackrest_restore_verify.sh --force."
fi

echo "pgBackRest rollout complete; BACKUP_MODE=pgbackrest"
