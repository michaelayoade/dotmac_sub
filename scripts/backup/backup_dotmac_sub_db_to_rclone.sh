#!/usr/bin/env bash

set -euo pipefail

# Backward-compatible wrapper for the multi-database backup job.
exec "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/backup_dotmac_sub_dbs_to_rclone.sh" "$@"
