#!/usr/bin/env bash

set -euo pipefail

REMOTE_NAME="${REMOTE_NAME:-Backup}"
REMOTE_BASE_PATH="${REMOTE_BASE_PATH:-db.backup/dotmac_sub}"
KEEP_LAST="${KEEP_LAST:-5}"
TMP_DIR="${TMP_DIR:-/root/rclone-backups/dotmac_sub}"

timestamp="$(date -u +%Y-%m-%dT%H-%M-%SZ)"

declare -a cleanup_paths=()

cleanup() {
  if (( ${#cleanup_paths[@]} > 0 )); then
    rm -f "${cleanup_paths[@]}"
  fi
}

trap cleanup EXIT

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Missing required command: %s\n' "$1" >&2
    exit 1
  fi
}

trim_remote_backups() {
  local remote_path="$1"
  local -a remote_backups=()
  local old_backup=""

  mapfile -t remote_backups < <(rclone lsf --files-only "${REMOTE_NAME}:${remote_path}" | sort)

  if (( ${#remote_backups[@]} > KEEP_LAST )); then
    local delete_count=$(( ${#remote_backups[@]} - KEEP_LAST ))
    for old_backup in "${remote_backups[@]:0:${delete_count}}"; do
      rclone deletefile "${REMOTE_NAME}:${remote_path}/${old_backup}"
    done
  fi
}

backup_postgres() {
  local container_name="$1"
  local db_name="$2"
  local folder_name="$3"
  local remote_path="${REMOTE_BASE_PATH}/${folder_name}"
  local backup_name="${folder_name}_${timestamp}.sql.gz"
  local local_backup_path="${TMP_DIR}/${backup_name}"

  mkdir -p "${TMP_DIR}"
  cleanup_paths+=("${local_backup_path}")

  rclone mkdir "${REMOTE_NAME}:${remote_path}"

  docker exec \
    -e BACKUP_DB_NAME="${db_name}" \
    "${container_name}" \
    sh -lc '
    export PGPASSWORD="${POSTGRES_PASSWORD}"
    exec pg_dump -U "${POSTGRES_USER}" -d "${BACKUP_DB_NAME}"
  ' | gzip > "${local_backup_path}"

  rclone copyto "${local_backup_path}" "${REMOTE_NAME}:${remote_path}/${backup_name}"
  trim_remote_backups "${remote_path}"

  printf 'PostgreSQL backup uploaded to %s:%s/%s\n' "${REMOTE_NAME}" "${remote_path}" "${backup_name}"
}

backup_mongodb() {
  local container_name="$1"
  local db_name="$2"
  local folder_name="$3"
  local mongo_user="$4"
  local mongo_password="$5"
  local remote_path="${REMOTE_BASE_PATH}/${folder_name}"
  local backup_name="${folder_name}_${timestamp}.archive.gz"
  local local_backup_path="${TMP_DIR}/${backup_name}"

  mkdir -p "${TMP_DIR}"
  cleanup_paths+=("${local_backup_path}")

  rclone mkdir "${REMOTE_NAME}:${remote_path}"

  docker exec \
    -e BACKUP_MONGO_USER="${mongo_user}" \
    -e BACKUP_MONGO_PASSWORD="${mongo_password}" \
    -e BACKUP_MONGO_DB="${db_name}" \
    "${container_name}" \
    sh -lc '
    exec mongodump \
      --username "${BACKUP_MONGO_USER}" \
      --password "${BACKUP_MONGO_PASSWORD}" \
      --authenticationDatabase admin \
      --db "${BACKUP_MONGO_DB}" \
      --archive \
      --gzip
  ' > "${local_backup_path}"

  rclone copyto "${local_backup_path}" "${REMOTE_NAME}:${remote_path}/${backup_name}"
  trim_remote_backups "${remote_path}"

  printf 'MongoDB backup uploaded to %s:%s/%s\n' "${REMOTE_NAME}" "${remote_path}" "${backup_name}"
}

require_cmd docker
require_cmd rclone

backup_postgres "dotmac_sub_db" "dotmac_sub" "dotmac_sub_db"
backup_postgres "dotmac_sub_radius_db" "radius" "dotmac_sub_radius_db"
backup_mongodb \
  "dotmac_sub_genieacs_mongodb" \
  "genieacs" \
  "dotmac_sub_genieacs_mongodb" \
  "${GENIEACS_MONGODB_USER:-genieacs}" \
  "${GENIEACS_MONGODB_PASSWORD:-$(grep '^GENIEACS_MONGODB_PASSWORD=' /opt/dotmac_sub/.env | cut -d= -f2-)}"
