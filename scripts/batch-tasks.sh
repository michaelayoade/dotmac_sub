#!/usr/bin/env bash
# Thin wrapper — delegates to shared canonical batch-tasks.sh
export SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
exec /home/dotmac/projects/shared-scripts/batch-tasks.sh "$@"
