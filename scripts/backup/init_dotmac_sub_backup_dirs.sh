#!/usr/bin/env bash

set -euo pipefail

REMOTE_NAME="${REMOTE_NAME:-Backup}"
REMOTE_BASE_PATH="${REMOTE_BASE_PATH:-db.backup/dotmac_sub}"

for folder_name in \
  dotmac_sub_db \
  dotmac_sub_radius_db \
  dotmac_sub_genieacs_mongodb
do
  rclone mkdir "${REMOTE_NAME}:${REMOTE_BASE_PATH}/${folder_name}"
  printf 'Ensured remote folder exists: %s:%s/%s\n' "${REMOTE_NAME}" "${REMOTE_BASE_PATH}" "${folder_name}"
done
