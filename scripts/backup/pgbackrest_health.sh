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
MAX_AGE_SECONDS="${PGBACKREST_MAX_BACKUP_AGE_SECONDS:-36000}"
LOCK_FILE="${PGBACKREST_LOCK_FILE:-/var/lock/dotmac_pgbackrest.lock}"
LOG_FILE="${PGBACKREST_LOG_FILE:-/var/log/dotmac_sub/pgbackrest-operations.log}"
VM_IMPORT_URL="${PGBACKREST_VM_IMPORT_URL:-http://127.0.0.1:8428/api/v1/import/prometheus}"
MODE="${1:-scheduled}"
if [[ "${MODE}" != "scheduled" && "${MODE}" != "--gate" ]]; then
  echo "usage: $0 [--gate]" >&2
  exit 2
fi

mkdir -p "$(dirname "${LOG_FILE}")"
exec > >(tee -a "${LOG_FILE}") 2>&1
exec 9>"${LOCK_FILE}"
if [[ "${MODE}" == "--gate" ]]; then
  if ! flock -w "${PGBACKREST_GATE_LOCK_WAIT_SECONDS:-60}" 9; then
    echo "BACKUP HEALTH FAILURE: backup/restore operation is still running" >&2
    exit 1
  fi
elif ! flock -n 9; then
  echo "event=database_backup_health status=skipped reason=operation_in_progress"
  exit 0
fi

status=0
emit_health_status() {
  local observed_at="$1"
  printf 'dotmac_database_backup_health_status{stanza="%s"} %s\n' "${STANZA}" "${status}" \
    | curl -fsS --max-time 5 --data-binary @- "${VM_IMPORT_URL}" >/dev/null || true
  echo "event=database_backup_health status=$([[ ${status} -eq 1 ]] && echo success || echo failure) observed_at=${observed_at}"
}
trap 'emit_health_status "$(date +%s)"' EXIT

docker exec --user postgres "${DB_CONTAINER}" pgbackrest --stanza="${STANZA}" check
info_json="$(docker exec --user postgres "${DB_CONTAINER}" pgbackrest --stanza="${STANZA}" --output=json info)"
health_tsv="$(
  printf '%s' "${info_json}" | python3 "${ROOT_DIR}/scripts/backup/pgbackrest_info.py" \
    --stanza "${STANZA}" --max-age-seconds "${MAX_AGE_SECONDS}" --format tsv
)"
IFS=$'\t' read -r backup_label backup_type completed_at age_seconds <<< "${health_tsv}"

archiver_tsv="$(
  docker exec --user postgres "${DB_CONTAINER}" psql -X -U postgres -d postgres -At -F $'\t' -c \
    "SELECT archived_count, failed_count, COALESCE(EXTRACT(EPOCH FROM now() - last_archived_time)::bigint, -1) FROM pg_stat_archiver"
)"
IFS=$'\t' read -r archived_count failed_count archive_age_seconds <<< "${archiver_tsv}"
if [[ "${archive_age_seconds}" -lt 0 ]]; then
  echo "BACKUP HEALTH FAILURE: PostgreSQL has not archived a WAL segment" >&2
  exit 1
fi

observed_at="$(date +%s)"
cat <<EOF | curl -fsS --max-time 5 --data-binary @- "${VM_IMPORT_URL}" >/dev/null || true
dotmac_database_backup_latest_timestamp_seconds{stanza="${STANZA}",type="${backup_type}"} ${completed_at}
dotmac_database_backup_latest_age_seconds{stanza="${STANZA}",type="${backup_type}"} ${age_seconds}
dotmac_database_wal_archived_total{stanza="${STANZA}"} ${archived_count}
dotmac_database_wal_archive_failures_total{stanza="${STANZA}"} ${failed_count}
dotmac_database_wal_archive_last_age_seconds{stanza="${STANZA}"} ${archive_age_seconds}
EOF
status=1
echo "backup_ok stanza=${STANZA} label=${backup_label} type=${backup_type} age_seconds=${age_seconds} archive_age_seconds=${archive_age_seconds}"
