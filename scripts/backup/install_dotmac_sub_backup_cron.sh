#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
backup_script="${script_dir}/backup_dotmac_sub_dbs_to_rclone.sh"
cron_line="40 3 * * * flock -n /var/lock/dotmac_sub_offsite_backup.lock nice -n 15 ionice -c 2 -n 7 ${backup_script} >> /var/log/dotmac_sub_db_backup.log 2>&1"

current_crontab="$(mktemp)"
trap 'rm -f "${current_crontab}"' EXIT

if crontab -l > "${current_crontab}" 2>/dev/null; then
  :
else
  : > "${current_crontab}"
fi

# This installer owns the one line invoking this exact script. Remove legacy
# schedules (including the old 18:00 business-hours run) before installing it.
sed -i "\|${backup_script}|d" "${current_crontab}"
printf '%s\n' "${cron_line}" >> "${current_crontab}"
crontab "${current_crontab}"
printf 'Installed cron entry: %s\n' "${cron_line}"
