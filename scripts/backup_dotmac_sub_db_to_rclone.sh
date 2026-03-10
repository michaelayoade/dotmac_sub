#!/usr/bin/env bash

set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-dotmac_sub_db}"
REMOTE_NAME="${REMOTE_NAME:-backup}"
REMOTE_PATH="${REMOTE_PATH:-db.backup/dotmac_sub_db}"
KEEP_LAST="${KEEP_LAST:-5}"
TMP_DIR="${TMP_DIR:-/root/rclone-backups/dotmac_sub_db}"

timestamp="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
backup_name="dotmac_sub_${timestamp}.sql.gz"
local_backup_path="${TMP_DIR}/${backup_name}"
remote_backup_path="${REMOTE_NAME}:${REMOTE_PATH}/${backup_name}"

mkdir -p "${TMP_DIR}"

cleanup() {
  rm -f "${local_backup_path}"
}

trap cleanup EXIT

docker exec "${CONTAINER_NAME}" sh -lc '
  export PGPASSWORD="${POSTGRES_PASSWORD}"
  exec pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}"
' | gzip > "${local_backup_path}"

rclone copyto "${local_backup_path}" "${remote_backup_path}"

mapfile -t remote_backups < <(rclone lsf --files-only "${REMOTE_NAME}:${REMOTE_PATH}" | sort)

if (( ${#remote_backups[@]} > KEEP_LAST )); then
  delete_count=$(( ${#remote_backups[@]} - KEEP_LAST ))
  for old_backup in "${remote_backups[@]:0:${delete_count}}"; do
    rclone deletefile "${REMOTE_NAME}:${REMOTE_PATH}/${old_backup}"
  done
fi

printf 'Backup uploaded to %s\n' "${remote_backup_path}"
