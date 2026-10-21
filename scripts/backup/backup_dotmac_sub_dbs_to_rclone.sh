#!/usr/bin/env bash

set -euo pipefail

# Single source for dump flags / bundle shape (globals + dump + manifest).
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/recovery_bundle_lib.sh"

REMOTE_NAME="${REMOTE_NAME:-Backup}"
REMOTE_BASE_PATH="${REMOTE_BASE_PATH:-db.backup/dotmac_sub}"
KEEP_LAST="${KEEP_LAST:-3}"
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
  # Retention is per BUNDLE, not per file: a bundle is dump + globals +
  # manifest sharing one stem. Counting files would keep KEEP_LAST files
  # (a single bundle) and could split a bundle across the cut.
  local remote_path="$1"
  local -a remote_files=() bundle_stems=()
  local file="" stem=""

  mapfile -t remote_files < <(rclone lsf --files-only "${REMOTE_NAME}:${remote_path}" | sort)

  for file in "${remote_files[@]}"; do
    case "${file}" in
      *.manifest.json) bundle_stems+=("${file%.manifest.json}") ;;
    esac
  done

  if (( ${#bundle_stems[@]} > KEEP_LAST )); then
    local delete_count=$(( ${#bundle_stems[@]} - KEEP_LAST ))
    for stem in "${bundle_stems[@]:0:${delete_count}}"; do
      rclone deletefile "${REMOTE_NAME}:${remote_path}/${stem}.sql.gz" || true
      rclone deletefile "${REMOTE_NAME}:${remote_path}/${stem}.globals.sql.gz" || true
      rclone deletefile "${REMOTE_NAME}:${remote_path}/${stem}.manifest.json" || true
    done
  fi

  # Legacy lone dumps (pre-bundle, no manifest): trim the oldest beyond
  # KEEP_LAST as before, so they age out instead of accumulating forever.
  local -a legacy_dumps=()
  for file in "${remote_files[@]}"; do
    case "${file}" in
      *.globals.sql.gz|*.manifest.json) continue ;;
      *.sql.gz)
        stem="${file%.sql.gz}"
        local is_bundle=0 known=""
        for known in "${bundle_stems[@]}"; do
          [[ "${known}" == "${stem}" ]] && is_bundle=1 && break
        done
        (( is_bundle == 0 )) && legacy_dumps+=("${file}")
        ;;
    esac
  done
  if (( ${#legacy_dumps[@]} > KEEP_LAST )); then
    local legacy_delete=$(( ${#legacy_dumps[@]} - KEEP_LAST ))
    for file in "${legacy_dumps[@]:0:${legacy_delete}}"; do
      rclone deletefile "${REMOTE_NAME}:${remote_path}/${file}"
    done
  fi
}

backup_postgres() {
  local container_name="$1"
  local db_name="$2"
  local folder_name="$3"
  local remote_path="${REMOTE_BASE_PATH}/${folder_name}"
  local stem="${folder_name}_${timestamp}"
  local backup_name="${stem}.sql.gz"
  local globals_name="${stem}.globals.sql.gz"
  local manifest_name="${stem}.manifest.json"
  local local_backup_path="${TMP_DIR}/${backup_name}"
  local local_globals_path="${TMP_DIR}/${globals_name}"
  local local_manifest_path="${TMP_DIR}/${manifest_name}"
  local tmp_suffix=".tmp.$$"

  mkdir -p "${TMP_DIR}"
  cleanup_paths+=("${local_backup_path}" "${local_globals_path}" "${local_manifest_path}" \
    "${local_backup_path}${tmp_suffix}" "${local_globals_path}${tmp_suffix}" \
    "${local_manifest_path}${tmp_suffix}")

  recovery_refuse_identity_stripping "${RECOVERY_PG_DUMP_FLAGS}" || return 1

  rclone mkdir "${REMOTE_NAME}:${remote_path}"

  # Globals FIRST, fail-closed (see recovery_bundle_lib.sh). Nothing is
  # uploaded unless the whole bundle exists locally.
  if ! docker exec \
    -e BACKUP_GLOBALS_FLAGS="${RECOVERY_GLOBALS_FLAGS}" \
    "${container_name}" \
    sh -lc '
    export PGPASSWORD="${POSTGRES_PASSWORD}"
    exec pg_dumpall -U "${POSTGRES_USER}" ${BACKUP_GLOBALS_FLAGS}
  ' | gzip > "${local_globals_path}${tmp_suffix}"; then
    recovery_fail_globals "${local_backup_path}" "pg_dumpall ${RECOVERY_GLOBALS_FLAGS} failed for ${container_name}"
  fi
  if [[ ! -s "${local_globals_path}${tmp_suffix}" ]]; then
    recovery_fail_globals "${local_backup_path}" "pg_dumpall produced an empty file for ${container_name}"
  fi
  if ! recovery_assert_no_password_material "${local_globals_path}${tmp_suffix}"; then
    recovery_fail_globals "${local_backup_path}" "globals contained password verifier material for ${container_name}"
  fi
  mv "${local_globals_path}${tmp_suffix}" "${local_globals_path}"

  # Ownership and GRANTs retained; the globals artifact above supplies the
  # roles they name. Flags come only from recovery_bundle_lib.sh.
  docker exec \
    -e BACKUP_DB_NAME="${db_name}" \
    -e BACKUP_PG_DUMP_FLAGS="${RECOVERY_PG_DUMP_FLAGS}" \
    "${container_name}" \
    sh -lc '
    export PGPASSWORD="${POSTGRES_PASSWORD}"
    exec pg_dump -U "${POSTGRES_USER}" -d "${BACKUP_DB_NAME}" ${BACKUP_PG_DUMP_FLAGS}
  ' | gzip > "${local_backup_path}${tmp_suffix}"
  mv "${local_backup_path}${tmp_suffix}" "${local_backup_path}"

  local pg_version=""
  pg_version="$(docker exec "${container_name}" pg_dump --version 2>/dev/null || echo unknown)"
  recovery_write_manifest "${local_manifest_path}${tmp_suffix}" "${db_name}" \
    "${local_globals_path}" "${local_backup_path}" "${pg_version}" "${GIT_SHA:-unknown}"
  mv "${local_manifest_path}${tmp_suffix}" "${local_manifest_path}"

  # Manifest uploads LAST: remotely, its presence certifies a complete bundle.
  rclone copyto "${local_globals_path}" "${REMOTE_NAME}:${remote_path}/${globals_name}"
  rclone copyto "${local_backup_path}" "${REMOTE_NAME}:${remote_path}/${backup_name}"
  rclone copyto "${local_manifest_path}" "${REMOTE_NAME}:${remote_path}/${manifest_name}"
  trim_remote_backups "${remote_path}"

  printf 'PostgreSQL recovery bundle uploaded to %s:%s/%s (+ globals + manifest)\n' \
    "${REMOTE_NAME}" "${remote_path}" "${backup_name}"
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
