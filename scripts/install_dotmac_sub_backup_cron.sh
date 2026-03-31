#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
backup_script="${script_dir}/backup_dotmac_sub_dbs_to_rclone.sh"
cron_line="0 18 * * * ${backup_script} >> /var/log/dotmac_sub_db_backup.log 2>&1"

current_crontab="$(mktemp)"
trap 'rm -f "${current_crontab}"' EXIT

if crontab -l > "${current_crontab}" 2>/dev/null; then
  :
else
  : > "${current_crontab}"
fi

if ! grep -Fqx "${cron_line}" "${current_crontab}"; then
  printf '%s\n' "${cron_line}" >> "${current_crontab}"
  crontab "${current_crontab}"
  printf 'Installed cron entry: %s\n' "${cron_line}"
else
  printf 'Cron entry already present: %s\n' "${cron_line}"
fi
