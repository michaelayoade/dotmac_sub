#!/usr/bin/env bash
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
STANZA="${PGBACKREST_STANZA:-dotmac-sub}"
LOCK_FILE="${PGBACKREST_LOCK_FILE:-/var/lock/dotmac_pgbackrest.lock}"
LOG_FILE="${PGBACKREST_LOG_FILE:-/var/log/dotmac_sub/pgbackrest-operations.log}"
VM_IMPORT_URL="${PGBACKREST_VM_IMPORT_URL:-http://127.0.0.1:8428/api/v1/import/prometheus}"
BACKUP_TYPE="${1:-incr}"
case "${BACKUP_TYPE}" in
  full|diff|incr) ;;
  *) echo "usage: $0 {full|diff|incr}" >&2; exit 2 ;;
esac

mkdir -p "$(dirname "${LOG_FILE}")"
exec > >(tee -a "${LOG_FILE}") 2>&1
command -v flock >/dev/null || { echo "Missing required command: flock" >&2; exit 1; }
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "event=database_backup status=skipped reason=operation_in_progress type=${BACKUP_TYPE}"
  exit 0
fi

emit_metrics() {
  local status="$1"
  local finished_at="$2"
  local duration="$3"
  local payload
  payload="dotmac_database_backup_run_status{stanza=\"${STANZA}\",type=\"${BACKUP_TYPE}\"} ${status}\n"
  payload+="dotmac_database_backup_last_duration_seconds{stanza=\"${STANZA}\",type=\"${BACKUP_TYPE}\"} ${duration}\n"
  if [[ "${status}" == "1" ]]; then
    payload+="dotmac_database_backup_last_success_timestamp_seconds{stanza=\"${STANZA}\",type=\"${BACKUP_TYPE}\"} ${finished_at}\n"
  else
    payload+="dotmac_database_backup_last_failure_timestamp_seconds{stanza=\"${STANZA}\",type=\"${BACKUP_TYPE}\"} ${finished_at}\n"
  fi
  printf '%b' "${payload}" | curl -fsS --max-time 5 --data-binary @- "${VM_IMPORT_URL}" >/dev/null || true
}

started_at="$(date +%s)"
on_exit() {
  local rc=$?
  local finished_at duration status
  finished_at="$(date +%s)"
  duration=$((finished_at - started_at))
  status=0
  [[ "${rc}" -eq 0 ]] && status=1
  emit_metrics "${status}" "${finished_at}" "${duration}"
  echo "event=database_backup status=$([[ ${rc} -eq 0 ]] && echo success || echo failure) type=${BACKUP_TYPE} duration_seconds=${duration} exit_code=${rc}"
}
trap on_exit EXIT

echo "event=database_backup status=started type=${BACKUP_TYPE} stanza=${STANZA}"
docker exec --user postgres "${DB_CONTAINER}" \
  nice -n 10 pgbackrest --stanza="${STANZA}" --type="${BACKUP_TYPE}" backup
