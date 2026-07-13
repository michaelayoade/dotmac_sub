#!/usr/bin/env bash
# Restore the latest backup into a disposable, network-isolated PostgreSQL.
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  set +a
fi

DB_CONTAINER="${DB_CONTAINER:-dotmac_pg_local}"
VERIFY_CONTAINER="${PGBACKREST_VERIFY_CONTAINER:-dotmac_pg_restore_verify}"
VERIFY_DB_NAME="${PGBACKREST_VERIFY_DB_NAME:-dotmac_sub}"
STANZA="${PGBACKREST_STANZA:-dotmac-sub}"
LOCK_FILE="${PGBACKREST_LOCK_FILE:-/var/lock/dotmac_pgbackrest.lock}"
RESTORE_DIR="${PGBACKREST_RESTORE_VERIFY_DIR:-/var/lib/dotmac-pgbackrest-restore-verify}"
REPO_DIR="${PGBACKREST_REPO_DIR:-/var/backups/pgbackrest}"
SPOOL_DIR="${PGBACKREST_SPOOL_DIR:-/var/lib/dotmac-pgbackrest-spool}"
SECRET_DIR="${PGBACKREST_SECRET_DIR:-/etc/dotmac/pgbackrest}"
LOG_FILE="${PGBACKREST_LOG_FILE:-/var/log/dotmac_sub/pgbackrest-operations.log}"
VM_IMPORT_URL="${PGBACKREST_VM_IMPORT_URL:-http://127.0.0.1:8428/api/v1/import/prometheus}"
MARKER="${RESTORE_DIR}/.dotmac-pgbackrest-restore-target"
FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ "${PGBACKREST_RESTORE_VERIFY_ENABLED:-false}" != "true" && "${FORCE}" -ne 1 ]]; then
  echo "event=database_restore_verify status=skipped reason=disabled"
  exit 0
fi
if [[ ! -f "${MARKER}" ]]; then
  echo "Refusing restore verification: safety marker is missing at ${MARKER}" >&2
  exit 1
fi

mkdir -p "$(dirname "${LOG_FILE}")"
exec > >(tee -a "${LOG_FILE}") 2>&1
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "event=database_restore_verify status=skipped reason=operation_in_progress"
  exit 0
fi

started_at="$(date +%s)"
verification_succeeded=0
cleanup() {
  local rc=$?
  local finished_at duration
  finished_at="$(date +%s)"
  duration=$((finished_at - started_at))
  docker rm -f "${VERIFY_CONTAINER}" >/dev/null 2>&1 || true
  if [[ "${PGBACKREST_KEEP_FAILED_RESTORE:-false}" != "true" || "${verification_succeeded}" -eq 1 ]]; then
    find "${RESTORE_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    touch "${MARKER}"
    chown "${PGBACKREST_POSTGRES_UID:-70}:${PGBACKREST_POSTGRES_GID:-70}" "${MARKER}"
    chmod 0600 "${MARKER}"
  fi
  cat <<EOF | curl -fsS --max-time 5 --data-binary @- "${VM_IMPORT_URL}" >/dev/null || true
dotmac_database_restore_verify_status{stanza="${STANZA}"} ${verification_succeeded}
dotmac_database_restore_verify_last_duration_seconds{stanza="${STANZA}"} ${duration}
EOF
  if [[ "${verification_succeeded}" -eq 1 ]]; then
    printf 'dotmac_database_restore_verify_last_success_timestamp_seconds{stanza="%s"} %s\n' "${STANZA}" "${finished_at}" \
      | curl -fsS --max-time 5 --data-binary @- "${VM_IMPORT_URL}" >/dev/null || true
  fi
  echo "event=database_restore_verify status=$([[ ${verification_succeeded} -eq 1 ]] && echo success || echo failure) duration_seconds=${duration} exit_code=${rc}"
}
trap cleanup EXIT

find "${RESTORE_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
echo "event=database_restore_verify status=restore_started stanza=${STANZA}"
docker exec --user postgres "${DB_CONTAINER}" \
  nice -n 10 pgbackrest --stanza="${STANZA}" \
  --pg1-path=/var/lib/postgresql/restore-verify \
  --type=immediate --target-action=promote restore

POSTGRES_IMAGE="$(docker inspect "${DB_CONTAINER}" --format '{{.Config.Image}}')"
docker run -d --rm \
  --name "${VERIFY_CONTAINER}" \
  --network none \
  -v "${RESTORE_DIR}:/var/lib/postgresql/data" \
  -v "${REPO_DIR}:/var/lib/pgbackrest:ro" \
  -v "${SPOOL_DIR}:/var/spool/pgbackrest" \
  -v "${ROOT_DIR}/config/pgbackrest/pgbackrest.conf:/etc/pgbackrest/pgbackrest.conf:ro" \
  -v "${SECRET_DIR}:/etc/pgbackrest/conf.d:ro" \
  "${POSTGRES_IMAGE}" \
  postgres -c archive_mode=off -c listen_addresses= -c unix_socket_directories=/tmp -c port=55432 \
  >/dev/null

deadline=$((SECONDS + ${PGBACKREST_RESTORE_START_TIMEOUT_SECONDS:-600}))
until docker exec "${VERIFY_CONTAINER}" pg_isready -h /tmp -p 55432 -U postgres >/dev/null 2>&1; do
  if ((SECONDS >= deadline)); then
    docker logs --tail 200 "${VERIFY_CONTAINER}" || true
    echo "Restored PostgreSQL did not become ready before timeout" >&2
    exit 1
  fi
  sleep 5
done

verify_result="$(
  docker exec "${VERIFY_CONTAINER}" psql -X -v ON_ERROR_STOP=1 -h /tmp -p 55432 \
    -U postgres -d "${VERIFY_DB_NAME}" -At -c \
    "SELECT CASE WHEN pg_is_in_recovery() THEN 'recovery' ELSE 'primary' END || ':' || count(*) FROM pg_catalog.pg_class"
)"
if [[ "${verify_result}" != primary:* ]]; then
  echo "Restored database sanity check failed: ${verify_result}" >&2
  exit 1
fi
verification_succeeded=1
echo "restore_ok stanza=${STANZA} database=${VERIFY_DB_NAME} result=${verify_result}"
